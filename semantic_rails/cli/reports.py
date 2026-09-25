"""Report builders behind the human CLI commands and the REPL."""

from __future__ import annotations

import argparse
import importlib
import shutil
import sys
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

from ..catalog_service import resolve_catalog
from ..config import list_package_paths, package_root_for_source, repo_root
from ..config_validation import PackageReference, parse_config_report, validate_config_report
from ..errors import SemanticLayerError
from ..local_config import local_profile_report
from ..package_tools import check_package_report, run_examples_report, run_package_tests_report
from ..planner import plan_payload
from ..runtime import Runtime, _normalize_query_limits
from .common import (
    _EXCLUDED_DISCOVERY_DIRS,
    _is_bundled_ref,
    _package_id_from_yaml,
    _ref_from_args,
    _ref_payload,
    _runtime_from_ref,
)
from .interpretation import _object_labels, describe_query

PROJECT_CHECK_MODES = ("parse", "runtime", "examples", "tests", "full")


CATALOG_KINDS = ("all", "entity", "dimension", "measure", "metric", "segment", "time")


def setup_report(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(_python_check())
    checks.append(_module_check("duckdb", package="duckdb"))
    checks.append(_module_check("yaml", package="PyYAML"))
    if args.server:
        checks.append(_module_check("uvicorn", package="uvicorn", optional=True))
    checks.append(_uv_check())

    packages = _registered_package_rows(with_status=False)
    checks.append(
        {
            "name": "registered_packages",
            "ok": True,
            "required": False,
            "summary": f"{len(packages)} package(s)",
            "packages": [row["id"] for row in packages],
        }
    )
    profile = local_profile_report()
    checks.append(
        {
            "name": "local_profile",
            "ok": True,
            "required": False,
            "summary": profile["package_path"] or "not configured",
            "path": profile["path"],
        }
    )

    package_check: dict[str, Any] | None = None
    if args.checks != "none":
        explicit_ref = bool(
            str(getattr(args, "package", "") or "").strip()
            or str(getattr(args, "path", "") or "").strip()
        )
        try:
            ref = _ref_from_args(args, allow_default=True)
        except SemanticLayerError as exc:
            if explicit_ref or "Local Semantic Rails profile" in str(exc):
                raise
            package_check = {
                "name": f"package_{args.checks}",
                "ok": True,
                "required": False,
                "summary": "no package selected",
                "message": "Run semantic-rails init my_package, then re-run setup with --path.",
            }
        else:
            if args.checks == "parse":
                package_check = _parse_check(ref)
            elif args.checks == "runtime":
                package_check = _runtime_check(ref)
            else:
                package_check = _full_check(ref)
            package_check["name"] = f"package_{args.checks}"
            package_check["required"] = True
        if package_check:
            checks.append(package_check)

    ok = all(bool(check.get("ok")) for check in checks if check.get("required", True))
    return {
        "ok": ok,
        "repo_root": repo_root(),
        "python": sys.version.split()[0],
        "checks": checks,
        "packages": packages,
        "local_profile": profile,
        "next_actions": _setup_next_actions(packages),
    }


def project_list_report(*, roots: Iterable[str] = (), with_status: bool = False) -> dict[str, Any]:
    rows = _registered_package_rows(with_status=with_status)
    seen = {str(Path(row["source_path"]).resolve()) for row in rows}

    for root in roots:
        for source in _discover_package_sources(Path(root).expanduser().resolve()):
            resolved = str(Path(source).resolve())
            if resolved in seen:
                continue
            row = _package_row_from_source(source, origin="discovered", with_status=with_status)
            rows.append(row)
            seen.add(resolved)

    rows.sort(key=lambda row: (str(row.get("id", "")), str(row.get("source_path", ""))))
    return {
        "ok": all(bool(row.get("ok", True)) for row in rows),
        "count": len(rows),
        "packages": rows,
    }


def list_objects_report(
    ref: PackageReference,
    *,
    resource_type: str = "all",
    search: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    if limit < 0:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "--limit must be greater than or equal to 0",
            details={"limit": limit},
        )
    runtime = _runtime_from_ref(ref)
    try:
        selected = _catalog_kind(resource_type)
        catalog = resolve_catalog(
            runtime,
            view="summary",
            verbosity="compact",
            kind=_runtime_catalog_kind(selected),
            search=search,
        )
        objects = _catalog_objects(catalog, selected=selected)
        return {
            "ok": True,
            "package": _runtime_package_payload(runtime, ref),
            "resource_type": selected,
            "search": search,
            "count": len(objects),
            "objects": objects[: max(0, limit)] if limit else objects,
            "truncated": bool(limit and len(objects) > limit),
            "catalog_counts": dict(catalog.get("counts", {}) or {}),
        }
    finally:
        runtime.close()


def ask_report(
    ref: PackageReference,
    *,
    question: str,
    execute: bool = False,
    compile_sql: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    if limit < 0:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "--limit must be greater than or equal to 0",
            details={"limit": limit},
        )
    runtime = _runtime_from_ref(ref)
    try:
        plan = plan_payload(runtime, intent=question, partial_query=None, limit=3, detail="best")
        query = _planned_query(plan)
        out: dict[str, Any] = {
            "ok": _payload_ok(plan) and bool(query),
            "package": _runtime_package_payload(runtime, ref),
            "question": question,
            "plan": _compact_plan(plan),
            "query": query,
        }
        if not _payload_ok(plan):
            errors = list(plan.get("errors", []) or [])
            if not errors and query:
                # A plan that fails validation keeps the reason in its diagnostics.
                errors = list(runtime.validate(query).get("errors", []) or [])
            if not errors and isinstance(plan.get("why"), dict):
                errors = [plan["why"]]
            out["errors"] = errors
            return out
        if not query:
            out["ok"] = False
            out["errors"] = [{"code": "NO_PLAN", "message": "No executable query was planned"}]
            return out
        # Restate the planned query itself, so a misread question is visible.
        out["interpretation"] = describe_query(query, _object_labels(runtime, plan))
        if execute:
            executable_query = dict(query)
            # A limit the planner put in the query itself, separate from --limit.
            planned_limit = query.get("limit")
            if not isinstance(planned_limit, int) or isinstance(planned_limit, bool):
                planned_limit = None
            # The executor recognizes a planned row fence even when --limit is 0.
            planned_row_limit = _normalize_query_limits(query.get("limits")).get("max_rows")
            row_limit = min((cap for cap in (limit, planned_row_limit) if cap), default=0)
            if row_limit:
                # Ask the warehouse for one row more than we show (keeping a smaller
                # planned limit) and fence at `row_limit`: `truncated` is then exact and
                # the warehouse still does top-N work.
                executable_query["limit"] = (
                    row_limit + 1 if planned_limit is None else min(planned_limit, row_limit + 1)
                )
                executable_query["limits"] = {
                    **dict(executable_query.get("limits", {}) or {}),
                    "max_rows": row_limit,
                }
            result = runtime.query(executable_query)
            rows = list(result.get("rows", []) or [])
            out["result"] = {
                "ok": bool(result.get("ok", True)),
                "rows": rows,
                "row_count": result.get("row_count", len(rows)),
                "row_limit": row_limit,
                "planned_limit": planned_limit,
                "planned_row_limit": planned_row_limit,
                "truncated": bool(result.get("truncated", False)),
                "output_columns": list(result.get("output_columns", []) or []),
                "warnings": list(result.get("warnings", []) or []),
                "assumptions": list(result.get("assumptions", []) or []),
            }
            out["ok"] = out["ok"] and bool(out["result"]["ok"])
        if compile_sql:
            compiled = runtime.compile(query)
            out["compile"] = {
                "ok": bool(compiled.get("ok", True)),
                "sql": compiled.get("rendered_sql", compiled.get("sql", "")),
                "dialect": compiled.get("dialect", runtime.warehouse),
                "output_columns": list(compiled.get("output_columns", []) or []),
                "warnings": list(compiled.get("warnings", []) or []),
            }
            out["ok"] = out["ok"] and bool(out["compile"]["ok"])
        return out
    finally:
        runtime.close()


def project_status_report(ref: PackageReference, *, checks: str = "parse") -> dict[str, Any]:
    root = Path(package_root_for_source(ref.source_path))
    files = _project_files(root)
    validation = project_validation_report(ref, mode=checks)
    summary = dict(validation.get("summary", {}) or {})
    parse = validation.get("checks", {}).get("parse", {}) or validation.get("parse", {})
    return {
        "ok": bool(validation.get("ok")),
        "package": _report_package(validation, ref),
        "source_path": ref.source_path,
        "project_root": str(root),
        "layout": "directory" if Path(ref.source_path).is_dir() else "single_file",
        "files": files,
        "summary": summary,
        "parse": parse,
        "checks": dict(validation.get("checks", {}) or {}),
        "next_actions": _status_next_actions(ref, validation),
    }


def project_validation_report(
    ref: PackageReference,
    *,
    mode: str = "full",
    compare_path: str = "",
    base_ref: str = "",
) -> dict[str, Any]:
    selected = str(mode or "full").strip().lower()
    if selected not in PROJECT_CHECK_MODES:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported validation mode '{mode}'",
            details={"supported_modes": list(PROJECT_CHECK_MODES)},
        )
    if selected == "parse":
        report, _ = parse_config_report(ref, progress=lambda _: None)
        return {
            "ok": bool(report.get("ok")),
            "package": _report_package(report, ref),
            "summary": {"parse": _parse_summary(report)},
            "checks": {"parse": _parse_summary(report)},
            "parse": report,
            "errors": list(report.get("errors", []) or []),
            "warnings": list(report.get("warnings", []) or []),
        }
    if selected == "runtime":
        report = validate_config_report(ref, progress=lambda _: None)
        return {
            "ok": bool(report.get("ok")),
            "package": _report_package(report, ref),
            "summary": {"runtime": _validate_summary(report)},
            "checks": {"runtime": _validate_summary(report)},
            "runtime": report,
            "errors": list(report.get("errors", []) or []),
        }
    if selected == "examples":
        report = run_examples_report(ref)
        return {
            "ok": bool(report.get("ok")),
            "package": _report_package(report, ref),
            "summary": {"examples": dict(report.get("summary", {}) or {})},
            "checks": {"examples": _ok_summary(report, "examples")},
            "examples": report,
            "errors": list(report.get("errors", []) or []),
        }
    if selected == "tests":
        report = run_package_tests_report(ref)
        return {
            "ok": bool(report.get("ok")),
            "package": _report_package(report, ref),
            "summary": {"tests": dict(report.get("summary", {}) or {})},
            "checks": {"tests": _ok_summary(report, "tests")},
            "tests": report,
            "errors": list(report.get("errors", []) or []),
        }

    report = check_package_report(ref, compare_path=compare_path, base_ref=base_ref)
    return {
        "ok": bool(report.get("ok")),
        "package": _report_package(report, ref),
        "summary": dict(report.get("summary", {}) or {}),
        "checks": _compact_full_checks(report),
        "check": report,
        "errors": _full_errors(report),
        "blockers": list(report.get("blockers", []) or []),
    }


def _runtime_package_payload(runtime: Runtime, ref: PackageReference) -> dict[str, Any]:
    return {
        "id": runtime.package_id or ref.package_id or _package_id_from_yaml(ref.source_path),
        "source_path": ref.source_path,
        "warehouse": runtime.warehouse,
        "bundled": _is_bundled_ref(ref),
    }


def _catalog_kind(value: str) -> str:
    text = str(value or "all").strip().lower()
    aliases = {
        "entities": "entity",
        "dimensions": "dimension",
        "measures": "measure",
        "metrics": "metric",
        "segments": "segment",
        "temporal_role": "time",
        "temporal_roles": "time",
        "times": "time",
    }
    return aliases.get(text, text)


def _runtime_catalog_kind(selected: str) -> str:
    if selected == "all":
        return ""
    if selected == "time":
        return "temporal_role"
    return selected


def _catalog_objects(catalog: dict[str, Any], *, selected: str) -> list[dict[str, Any]]:
    keys = {
        "entity": ["entities"],
        "dimension": ["dimensions"],
        "measure": ["measures"],
        "metric": ["metrics"],
        "segment": ["segments"],
        "time": ["temporal_roles"],
        "all": ["entities", "dimensions", "measures", "metrics", "segments", "temporal_roles"],
    }[selected]
    objects: list[dict[str, Any]] = []
    for key in keys:
        singular = "time" if key == "temporal_roles" else key.rstrip("s")
        for entry in list(catalog.get(key, []) or []):
            if not isinstance(entry, dict):
                continue
            objects.append(
                {
                    "kind": str(entry.get("object_type") or entry.get("kind") or singular),
                    "id": str(entry.get("id", "") or ""),
                    "label": str(
                        entry.get("label")
                        or entry.get("display_name")
                        or entry.get("name")
                        or entry.get("id")
                        or ""
                    ),
                    "description": str(entry.get("description", "") or ""),
                    "root_entity": str(entry.get("root_entity", "") or ""),
                    "available": bool(entry.get("available", True)),
                }
            )
    objects.sort(key=lambda row: (row["kind"], row["id"]))
    return objects


def _planned_query(plan: dict[str, Any]) -> dict[str, Any]:
    best = dict(plan.get("best", {}) or {})
    query = best.get("query_ir")
    if isinstance(query, dict):
        return dict(query)
    next_payload = dict(plan.get("next", {}) or {})
    validate = dict(next_payload.get("validate", {}) or {})
    query = validate.get("query")
    return dict(query) if isinstance(query, dict) else {}


def _compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    best = dict(plan.get("best", {}) or {})
    resolved = []
    for row in list(best.get("resolved", []) or []):
        if isinstance(row, dict):
            resolved.append(
                {
                    "id": row.get("id", ""),
                    "label": row.get("label", ""),
                    "kind": row.get("object_type", ""),
                }
            )
    return {
        "ok": _payload_ok(plan),
        "pattern": best.get("pattern", ""),
        "validation_ok": best.get("validation_ok", False),
        "resolved": resolved,
        "rationale": list(best.get("rationale", []) or []),
    }


def _payload_ok(payload: dict[str, Any]) -> bool:
    if isinstance(payload.get("ok"), bool):
        return bool(payload["ok"])
    status = str(payload.get("status", "") or "").lower()
    return status in {"", "ok", "success"} and not list(payload.get("errors", []) or [])


def _registered_package_rows(*, with_status: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for package_id, source_path in sorted(list_package_paths().items()):
        rows.append(
            _package_row_from_source(
                source_path,
                origin="registered",
                package_id=package_id,
                with_status=with_status,
            )
        )
    return rows


def _package_row_from_source(
    source_path: str | Path,
    *,
    origin: str,
    package_id: str = "",
    with_status: bool,
) -> dict[str, Any]:
    source = str(Path(source_path).resolve())
    row: dict[str, Any] = {
        "id": package_id or _package_id_from_yaml(source),
        "source_path": source,
        "origin": origin,
        "layout": "directory" if Path(source).is_dir() else "single_file",
    }
    if with_status:
        ref = PackageReference(source_path=source, package_id=package_id)
        report, _ = parse_config_report(ref, progress=lambda _: None)
        row["ok"] = bool(report.get("ok"))
        row["summary"] = dict(report.get("summary", {}) or {})
        row["errors"] = list(report.get("errors", []) or [])
        row["warnings"] = list(report.get("warnings", []) or [])
        if not row["id"]:
            row["id"] = str(report.get("package", {}).get("id", "") or "")
    else:
        row["ok"] = True
    return row


def _discover_package_sources(root: Path) -> list[str]:
    if not root.exists():
        raise SemanticLayerError("INVALID_CONFIG", f"Discovery root '{root}' does not exist")
    sources: list[str] = []
    for package_yml in sorted(root.rglob("package.yml")):
        if any(part in _EXCLUDED_DISCOVERY_DIRS for part in package_yml.parts):
            continue
        parent = package_yml.parent
        source = parent if (parent / "graph.yml").is_file() else package_yml
        sources.append(str(source))
    return sources


def _project_files(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _EXCLUDED_DISCOVERY_DIRS for part in Path(relative).parts):
            continue
        files.append({"path": relative, "bytes": path.stat().st_size})
    return files


def _python_check() -> dict[str, Any]:
    ok = sys.version_info >= (3, 11)
    return {
        "name": "python",
        "ok": ok,
        "required": True,
        "summary": sys.version.split()[0],
        "message": "Python 3.11 or newer is required.",
    }


def _module_check(module_name: str, *, package: str, optional: bool = False) -> dict[str, Any]:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        return {
            "name": package,
            "ok": False,
            "required": not optional,
            "summary": "missing",
            "message": str(exc),
        }
    version = ""
    with _suppress_metadata_errors():
        version = metadata.version(package)
    return {
        "name": package,
        "ok": True,
        "required": not optional,
        "summary": version or "installed",
    }


class _suppress_metadata_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is metadata.PackageNotFoundError


def _uv_check() -> dict[str, Any]:
    path = shutil.which("uv")
    return {
        "name": "uv",
        "ok": bool(path),
        "required": False,
        "summary": path or "not found",
        "message": "uv is optional for installed use, but recommended for source checkouts.",
    }


def _parse_check(ref: PackageReference) -> dict[str, Any]:
    report, _ = parse_config_report(ref, progress=lambda _: None)
    return {
        "ok": bool(report.get("ok")),
        "summary": _parse_summary(report),
        "errors": list(report.get("errors", []) or []),
        "warnings": list(report.get("warnings", []) or [])[:5],
    }


def _runtime_check(ref: PackageReference) -> dict[str, Any]:
    report = validate_config_report(ref, progress=lambda _: None)
    return {
        "ok": bool(report.get("ok")),
        "summary": _validate_summary(report),
        "errors": list(report.get("errors", []) or []),
    }


def _full_check(ref: PackageReference) -> dict[str, Any]:
    report = check_package_report(ref)
    return {
        "ok": bool(report.get("ok")),
        "summary": dict(report.get("summary", {}) or {}),
        "blockers": list(report.get("blockers", []) or []),
    }


def _parse_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = dict(report.get("summary", {}) or {})
    return {
        "ok": bool(report.get("ok")),
        "entities": int(summary.get("entities", 0) or 0),
        "measures": int(summary.get("measures", 0) or 0),
        "metrics": int(summary.get("metric_recipes", summary.get("metrics", 0)) or 0),
        "warnings": int(summary.get("warnings", len(report.get("warnings", []) or [])) or 0),
        "errors": int(summary.get("errors", len(report.get("errors", []) or [])) or 0),
    }


def _validate_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = dict(report.get("summary", {}) or {})
    return {
        "ok": bool(report.get("ok")),
        "probes_total": int(summary.get("probes_total", 0) or 0),
        "passed": int(summary.get("passed", 0) or 0),
        "failed": int(summary.get("failed", 0) or 0),
    }


def _ok_summary(report: dict[str, Any], key: str) -> dict[str, Any]:
    summary = dict(report.get("summary", {}) or {})
    summary["ok"] = bool(report.get("ok"))
    if key == "examples":
        summary.setdefault("examples_total", 0)
    if key == "tests":
        summary.setdefault("tests_total", 0)
    return summary


def _compact_full_checks(report: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, summary in dict(report.get("summary", {}) or {}).items():
        if isinstance(summary, dict):
            checks[name] = dict(summary)
    return checks


def _full_errors(report: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for name, check in dict(report.get("checks", {}) or {}).items():
        if not isinstance(check, dict):
            continue
        for error in list(check.get("errors", []) or []):
            errors.append({"check": name, **dict(error)})
    return errors


def _report_package(report: dict[str, Any], ref: PackageReference) -> dict[str, Any]:
    return {**dict(report.get("package", {}) or _ref_payload(ref)), "bundled": _is_bundled_ref(ref)}


def _setup_next_actions(packages: list[dict[str, Any]]) -> list[str]:
    actions = ["semantic-rails project list"]
    if packages:
        actions.append(f"semantic-rails project status --package {packages[0]['id']}")
    actions.extend(
        [
            "semantic-rails init my_package --yes",
            "semantic-rails profile init --package-path ./my_package",
            'semantic-rails ask --path ./my_package "total amount by event type" --run',
            "semantic-rails mcp setup --path ./my_package",
        ]
    )
    return actions


def _status_next_actions(ref: PackageReference, validation: dict[str, Any]) -> list[str]:
    source = ref.package_id and f"--package {ref.package_id}" or f"--path {ref.source_path}"
    if not validation.get("ok"):
        return [f"semantic-rails project validate {source} --mode full --json"]
    return [
        f"semantic-rails catalog {source}",
        f"semantic-rails project validate {source}",
        f"semantic-rails mcp setup {source}",
    ]
