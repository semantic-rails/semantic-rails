#!/usr/bin/env python3
"""Verify PyPI published the exact release artifacts, then smoke the wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

PROJECT = "semantic-rails"
USER_AGENT = "semantic-rails-post-publish-verifier/1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_artifacts(dist_dir: Path, version: str) -> dict[str, str]:
    expected = {
        f"semantic_rails-{version}-py3-none-any.whl",
        f"semantic_rails-{version}.tar.gz",
    }
    found = {path.name: _sha256(path) for path in dist_dir.iterdir() if path.name in expected}
    if set(found) != expected:
        raise RuntimeError(
            f"expected release artifacts {sorted(expected)}, found {sorted(found)} in {dist_dir}"
        )
    return found


def _published_artifacts(version: str) -> dict[str, str]:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{PROJECT}/{version}/json",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # nosec - fixed PyPI URL.
        payload = json.loads(response.read().decode("utf-8"))
    return {
        str(row.get("filename", "")): str((row.get("digests") or {}).get("sha256", ""))
        for row in list(payload.get("urls", []) or [])
    }


def _wait_for_exact_publish(
    expected: dict[str, str], version: str, *, attempts: int, delay: float
) -> None:
    last_error = "release not visible"
    for attempt in range(1, attempts + 1):
        try:
            published = _published_artifacts(version)
            expected_names = set(expected)
            published_names = set(published)
            missing = sorted(expected_names - published_names)
            unexpected = sorted(published_names - expected_names)
            mismatches = {
                name: {"expected": digest, "published": published.get(name, "missing")}
                for name, digest in expected.items()
                if published.get(name) != digest
            }
            if not missing and not unexpected and not mismatches:
                return
            last_error = (
                f"release file-set/digest mismatch: missing={missing}, "
                f"unexpected={unexpected}, digest_mismatches={mismatches}"
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        if attempt < attempts:
            time.sleep(delay)
    raise RuntimeError(f"PyPI verification failed after {attempts} attempts: {last_error}")


def _venv_python(root: Path) -> Path:
    candidate = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate


def _run_published_smoke(version: str, *, index_url: str) -> None:
    smoke = r"""
import json
from importlib.metadata import version as installed_version
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

expected = __EXPECTED_VERSION__
assert installed_version("semantic-rails") == expected
runtime = Runtime("jaffle_shop")
try:
    planned = plan_payload(runtime, intent="orders by store", detail="best", limit=1)
    assert planned.get("status") == "ok", planned
    best = planned.get("best") or {}
    assert best.get("validation_ok") is True, planned
    query = dict(best.get("query_ir") or {})
    assert query.get("group_by") == ["dimension.jaffle_store_name"], query
    assert "time" not in query, query
    query["limit"] = 3
    validated = runtime.validate(query)
    compiled = runtime.compile(query)
    executed = runtime.query(query)
    assert validated.get("ok") is True, validated
    assert compiled.get("ok") is True and compiled.get("rendered_sql"), compiled
    assert executed.get("ok") is True and executed.get("rows"), executed
    print(json.dumps({"version": expected, "rows": executed["row_count"]}))
finally:
    runtime.close()
""".replace("__EXPECTED_VERSION__", repr(version))

    with tempfile.TemporaryDirectory(prefix="semantic-rails-pypi-smoke-") as tmp:
        root = Path(tmp)
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = _venv_python(root / "venv")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--retries",
                "5",
                "--timeout",
                "15",
                "--index-url",
                index_url,
                f"{PROJECT}=={version}",
            ],
            cwd=root,
            env=env,
            check=True,
        )
        subprocess.run([str(python), "-c", smoke], cwd=root, env=env, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--index-url", default="https://pypi.org/simple")
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay", type=float, default=10.0)
    args = parser.parse_args(argv)

    expected = _local_artifacts(args.dist_dir.resolve(), args.version)
    _wait_for_exact_publish(
        expected,
        args.version,
        attempts=max(1, args.attempts),
        delay=max(0.0, args.delay),
    )
    _run_published_smoke(args.version, index_url=args.index_url)
    print(f"Published release verified: {PROJECT}=={args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
