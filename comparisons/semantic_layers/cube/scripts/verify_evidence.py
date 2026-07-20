"""Verify the captured Cube comparison evidence without installing npm packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
PROJECT_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "cube"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path.relative_to(REPO_ROOT)}: {exc}")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"{path.relative_to(REPO_ROOT)}: expected a JSON object")
        return {}
    return payload


def _check_hash(
    path: Path,
    expected: str,
    errors: list[str],
    *,
    label: str,
) -> None:
    if not path.is_file():
        errors.append(f"{label}: missing {path.relative_to(REPO_ROOT)}")
        return
    actual = _sha256(path)
    if actual != expected:
        errors.append(f"{label}: sha256 {actual} != recorded {expected}")


def _verify_source_artifacts(snapshot: dict[str, Any], errors: list[str]) -> None:
    if (PROJECT_DIR / "package.json").exists() or (PROJECT_DIR / "package-lock.json").exists():
        errors.append(
            "canonical package.json/package-lock.json must remain absent (no npm install surface)"
        )

    integrity = dict(snapshot.get("source_integrity", {}) or {})
    package_path = PROJECT_DIR / "original-package.json.evidence"
    lock_path = PROJECT_DIR / "original-package-lock.json.evidence"
    _check_hash(
        package_path,
        str(integrity.get("package_json_sha256", "")),
        errors,
        label="original package manifest",
    )
    _check_hash(
        lock_path,
        str(integrity.get("package_lock_sha256", "")),
        errors,
        label="original package lock",
    )
    package = _load_json(package_path, errors)
    lock = _load_json(lock_path, errors)
    if package.get("private") is not True:
        errors.append("original package evidence must retain private=true")
    if int(lock.get("lockfileVersion", 0) or 0) != int(integrity.get("lockfile_version", 0) or 0):
        errors.append("package lock version does not match runtime-snapshot.json")


def _verify_audit(snapshot: dict[str, Any], errors: list[str]) -> None:
    audit_path = PROJECT_DIR / "npm-audit.json.evidence"
    artifact = dict(snapshot.get("evidence_artifacts", {}) or {}).get(audit_path.name, {})
    _check_hash(
        audit_path,
        str(dict(artifact or {}).get("sha256", "")),
        errors,
        label="raw npm audit",
    )
    payload = _load_json(audit_path, errors)
    metadata = dict(payload.get("metadata", {}) or {})
    actual_vulnerabilities = dict(metadata.get("vulnerabilities", {}) or {})
    expected_vulnerabilities = dict(
        dict(snapshot.get("audit", {}) or {}).get("vulnerabilities", {}) or {}
    )
    for key in ("critical", "high", "moderate", "low", "total"):
        if int(actual_vulnerabilities.get(key, -1)) != int(expected_vulnerabilities.get(key, -2)):
            errors.append(f"npm audit vulnerability count mismatch for {key}")

    actual_dependencies = dict(metadata.get("dependencies", {}) or {})
    expected_dependencies = dict(
        dict(snapshot.get("audit", {}) or {}).get("dependencies", {}) or {}
    )
    dependency_keys = {
        "production": "prod",
        "development": "dev",
        "optional": "optional",
        "total": "total",
    }
    for expected_key, actual_key in dependency_keys.items():
        if int(actual_dependencies.get(actual_key, -1)) != int(
            expected_dependencies.get(expected_key, -2)
        ):
            errors.append(f"npm audit dependency count mismatch for {expected_key}")


def _verify_sbom(snapshot: dict[str, Any], errors: list[str]) -> None:
    sbom_path = PROJECT_DIR / "sbom.cdx.json.evidence"
    artifact = dict(snapshot.get("evidence_artifacts", {}) or {}).get(sbom_path.name, {})
    expected_artifact_hash = str(dict(artifact or {}).get("sha256", ""))
    _check_hash(sbom_path, expected_artifact_hash, errors, label="normalized CycloneDX SBOM")
    payload = _load_json(sbom_path, errors)
    if "serialNumber" in payload or "timestamp" in dict(payload.get("metadata", {}) or {}):
        errors.append("normalized SBOM still contains nondeterministic identity/timestamp fields")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    canonical_hash = hashlib.sha256(canonical).hexdigest()
    sbom = dict(snapshot.get("sbom", {}) or {})
    if canonical_hash != str(sbom.get("normalized_sha256", "")):
        errors.append("normalized SBOM canonical hash does not match runtime-snapshot.json")
    if canonical_hash != expected_artifact_hash:
        errors.append("normalized SBOM artifact hash does not match its canonical JSON hash")
    if str(payload.get("specVersion", "")) != str(sbom.get("spec_version", "")):
        errors.append("CycloneDX spec version does not match runtime-snapshot.json")
    if len(list(payload.get("components", []) or [])) != int(sbom.get("component_count", -1)):
        errors.append("CycloneDX component count does not match runtime-snapshot.json")


def _verify_comparison_manifest(snapshot: dict[str, Any], errors: list[str]) -> None:
    evidence = dict(snapshot.get("comparison_evidence", {}) or {})
    manifest_path = REPO_ROOT / str(evidence.get("manifest", ""))
    _check_hash(
        manifest_path,
        str(evidence.get("manifest_sha256", "")),
        errors,
        label="comparison evidence manifest",
    )
    manifest = _load_json(manifest_path, errors)
    files = dict(manifest.get("files", {}) or {})
    if len(files) != int(evidence.get("file_count", -1)):
        errors.append("comparison evidence manifest file count does not match snapshot")
    allowed_roots = (
        "comparisons/semantic_layers/cube/index.js",
        "comparisons/semantic_layers/cube/model/",
        "comparisons/semantic_layers/cube/queries/",
        "comparisons/semantic_layers/cube/scripts/run_questions.py",
        "comparisons/semantic_layers/shared/results/cube/",
    )
    for relative, expected_hash in sorted(files.items()):
        if not any(relative == root or relative.startswith(root) for root in allowed_roots):
            errors.append(f"comparison manifest contains unexpected path: {relative}")
            continue
        path = (REPO_ROOT / relative).resolve()
        if not path.is_relative_to(REPO_ROOT.resolve()):
            errors.append(f"comparison manifest escapes repository: {relative}")
            continue
        _check_hash(path, str(expected_hash), errors, label=relative)

    query_paths = [path for path in files if "/cube/queries/q" in path]
    if len(query_paths) != 16:
        errors.append(f"expected 16 captured Cube queries, found {len(query_paths)}")
    summary_path = "comparisons/semantic_layers/shared/results/cube/summary.json"
    summary = _load_json(REPO_ROOT / summary_path, errors)
    questions = list(summary.get("questions", []) or [])
    if len(questions) != 16:
        errors.append(f"expected 16 captured Cube result records, found {len(questions)}")
    for row in questions:
        if not isinstance(row, dict):
            errors.append("Cube summary contains a non-object question record")
            continue
        for key in ("query_path", "result_path", "sql_path"):
            relative = str(row.get(key, "") or "")
            if relative not in files:
                errors.append(f"Cube summary {key} is not hash-pinned: {relative}")


def main() -> int:
    errors: list[str] = []
    snapshot = _load_json(PROJECT_DIR / "runtime-snapshot.json", errors)
    _verify_source_artifacts(snapshot, errors)
    _verify_audit(snapshot, errors)
    _verify_sbom(snapshot, errors)
    _verify_comparison_manifest(snapshot, errors)
    if errors:
        print("Cube captured-evidence verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    evidence = dict(snapshot.get("comparison_evidence", {}) or {})
    print(
        "Cube captured evidence verified offline: "
        f"{evidence.get('file_count', 0)} source/result files, raw npm audit, "
        "normalized CycloneDX SBOM, and non-installable original lock graph."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
