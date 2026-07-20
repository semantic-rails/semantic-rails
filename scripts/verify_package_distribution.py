#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path
from tarfile import open as open_tar
from typing import Any
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_WHEEL_MEMBERS = {
    "semantic_rails/__init__.py",
    "semantic_rails/compiler_parts/__init__.py",
    "semantic_rails/config_parts/__init__.py",
    "semantic_rails/contracts/__init__.py",
    "semantic_rails/metadata_parts/__init__.py",
    "semantic_rails/runtime_parts/__init__.py",
}

REQUIRED_WHEEL_SUFFIXES = {
    "share/semantic-rails/configs/semantic_rails/jaffle_shop/package.yml",
    "share/semantic-rails/configs/semantic_rails/jaffle_shop/examples/core.yml",
    "share/semantic-rails/configs/semantic_rails/jaffle_shop/models/core/orders.yml",
    "share/semantic-rails/configs/semantic_rails/jaffle_shop/metrics/core/core_metrics.yml",
    "share/semantic-rails/configs/semantic_rails/jaffle_shop/segments/core.yml",
    # data/jaffle_shop.duckdb is intentionally NOT shipped in the wheel. The
    # runtime seeds it from raw_*.csv + seed_jaffle.sql on first use, so
    # bundling the file would (a) bloat the wheel and (b) break the build on
    # a clean git checkout where the file is gitignored.
    "share/semantic-rails/data/jaffle_csv/raw_orders.csv",
    "share/semantic-rails/data/seed_jaffle.sql",
    "semantic_rails/contracts/architect_mcp.v1.json",
    "semantic_rails/contracts/http_api.v1.openapi.json",
    "semantic_rails/contracts/package.v1.json",
    "semantic_rails/contracts/query_ir.preview.v2.json",
    "semantic_rails/contracts/query_ir.v1.json",
    "semantic_rails/contracts/query_mcp.v1.json",
    "semantic_rails/contracts/semantic_contract.v1.json",
    "semantic_rails/contracts/validation_report.v1.json",
}

FORBIDDEN_WHEEL_MEMBER_PATTERNS = {
    "bytecode cache": re.compile(r"(^|/)(?:__pycache__/|[^/]+\.py[co]$)"),
    "egg-info residue": re.compile(r"(^|/)[^/]+\.egg-info/"),
    "duplicate local metadata": re.compile(r"(^|/)[^/]+ 2(?:\.[^/]*)?$"),
}

FORBIDDEN_RELEASE_MEMBER_PATTERNS = {
    "private website root": re.compile(r"(^|/)website/"),
    "private deployment root": re.compile(r"(^|/)deploy/cloudflare/"),
    "private deployment workflow": re.compile(r"(^|/)\.github/workflows/deploy-cloudflare\.yml$"),
    "private site smoke": re.compile(r"(^|/)scripts/smoke_public_demo\.py$"),
    "private site dev server": re.compile(r"(^|/)scripts/dev/try_live_server\.py$"),
    "private hosted-demo policy": re.compile(r"(^|/)semantic_rails/public_demo\.py$"),
    "private product client": re.compile(r"(^|/)semantic_rails/cloud_client\.py$"),
    "private product client tests": re.compile(r"(^|/)tests/semantic_rails/test_cloud_client\.py$"),
    "private product client docs": re.compile(r"(^|/)docs/CLOUD_CHANGE_WORKFLOW\.md$"),
}

FORBIDDEN_ENGINE_SYMBOL_PATTERNS = {
    "private client module": re.compile(r"\bsemantic_rails\.cloud_client\b"),
    "private client type": re.compile(r"\b(?:CloudClient|CloudProfileContext)\b"),
    "private client helper": re.compile(
        r"\b(?:cloud_(?:connect|status|pull|push|adopt)_report|"
        r"init_cloud_profile|resolve_cloud_profile|update_cloud_profile_base|"
        r"cloud_profile_candidate|cloud_profile_report)\b"
    ),
    "private CLI command": re.compile(
        r"(?:sub|cloud_sub)\.add_parser\(\s*[\"']cloud[\"']|"
        r"\bcommand\s*==\s*[\"']cloud[\"']"
    ),
    "private hosted-demo symbol": re.compile(
        r"\bSEMANTIC_RAILS_PUBLIC_DEMO\b|\bpublic_demo_enabled\b|"
        r"\benforce_public_demo_(?:http_payload|mcp_message)\b"
    ),
}


def _forbidden_members(names: set[str]) -> dict[str, list[str]]:
    matches = {
        label: sorted(name for name in names if pattern.search(name))
        for label, pattern in FORBIDDEN_RELEASE_MEMBER_PATTERNS.items()
    }
    return {label: members for label, members in matches.items() if members}


def _is_engine_source(name: str) -> bool:
    return name.startswith("semantic_rails/") or "/semantic_rails/" in name


def _forbidden_engine_symbols(
    names: set[str],
    read_member: Any,
) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for name in sorted(names):
        if not name.endswith(".py") or not _is_engine_source(name):
            continue
        try:
            text = read_member(name).decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            continue
        for label, pattern in FORBIDDEN_ENGINE_SYMBOL_PATTERNS.items():
            if pattern.search(text):
                matches.setdefault(label, []).append(name)
    return matches


def _run(
    cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        print(f"Command failed: {' '.join(cmd)}")
        if result.stdout.strip():
            print("stdout:")
            print(result.stdout)
        if result.stderr.strip():
            print("stderr:")
            print(result.stderr)
        raise SystemExit(result.returncode)
    return result


def _venv_python(venv_dir: Path) -> Path:
    unix = venv_dir / "bin" / "python"
    if unix.exists():
        return unix
    return venv_dir / "Scripts" / "python.exe"


def _create_smoke_venv(venv_dir: Path) -> None:
    uv = shutil.which("uv")
    if uv:
        _run([uv, "venv", "--seed", "--python", sys.executable, str(venv_dir)], cwd=REPO_ROOT)
        return
    venv.EnvBuilder(with_pip=True).create(venv_dir)


def _project_version() -> str:
    payload = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def _build_distribution(output_dir: Path) -> None:
    _run(["uv", "build", "--out-dir", str(output_dir)], cwd=REPO_ROOT)


def _release_artifacts(output_dir: Path) -> tuple[Path, Path]:
    version = _project_version()
    # The PyPI distribution is `semantic-rails` (wheel name `semantic_rails`),
    # while the import package is `semantic_rails`. Match either form to stay
    # compatible if the dist is renamed in the future.
    wheels = sorted(output_dir.glob("semantic_rails-*.whl"))
    sdists = sorted(output_dir.glob("semantic_rails-*.tar.gz"))
    expected_wheel = f"semantic_rails-{version}-py3-none-any.whl"
    expected_sdist = f"semantic_rails-{version}.tar.gz"
    if [path.name for path in wheels] != [expected_wheel] or [path.name for path in sdists] != [
        expected_sdist
    ]:
        print("Package distribution check failed:")
        print(
            " - expected exactly one wheel and one sdist for project version "
            f"{version}: {expected_wheel}, {expected_sdist}"
        )
        print(f" - found wheels: {[path.name for path in wheels]}")
        print(f" - found sdists: {[path.name for path in sdists]}")
        raise SystemExit(1)
    return wheels[0], sdists[0]


def _assert_wheel_contents(wheel: Path) -> None:
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        private_symbols = _forbidden_engine_symbols(names, archive.read)

    forbidden = {
        label: sorted(name for name in names if pattern.search(name))
        for label, pattern in FORBIDDEN_WHEEL_MEMBER_PATTERNS.items()
    }
    forbidden = {label: matches for label, matches in forbidden.items() if matches}
    forbidden.update(_forbidden_members(names))
    forbidden.update(private_symbols)
    missing = sorted(REQUIRED_WHEEL_MEMBERS - names)
    missing_suffixes = sorted(
        suffix
        for suffix in REQUIRED_WHEEL_SUFFIXES
        if not any(name.endswith(suffix) for name in names)
    )
    if missing or missing_suffixes or forbidden:
        print("Package distribution check failed:")
        print(f" - wheel contents are not release-clean: {wheel.name}")
        for member in missing:
            print(f"   - missing required member: {member}")
        for suffix in missing_suffixes:
            print(f"   - missing required suffix: *{suffix}")
        for label, matches in forbidden.items():
            print(f"   - forbidden {label}:")
            for member in matches:
                print(f"     - {member}")
        raise SystemExit(1)


def _assert_sdist_contents(sdist: Path) -> None:
    with open_tar(sdist, "r:gz") as archive:
        names = set(archive.getnames())

        def read_member(name: str) -> bytes:
            member = archive.extractfile(name)
            return member.read() if member is not None else b""

        private_symbols = _forbidden_engine_symbols(names, read_member)
    root = f"semantic_rails-{_project_version()}"
    required = {
        f"{root}/pyproject.toml",
        f"{root}/README.md",
        f"{root}/LICENSE",
        f"{root}/NOTICE",
        f"{root}/semantic_rails/__init__.py",
        *{
            f"{root}/semantic_rails/contracts/{Path(suffix).name}"
            for suffix in REQUIRED_WHEEL_SUFFIXES
            if suffix.startswith("semantic_rails/contracts/")
        },
    }
    missing = sorted(required - names)
    forbidden: dict[str, list[str]] = {
        "bytecode cache": sorted(
            name for name in names if "/__pycache__/" in name or name.endswith((".pyc", ".pyo"))
        )
    }
    forbidden = {label: members for label, members in forbidden.items() if members}
    forbidden.update(_forbidden_members(names))
    forbidden.update(private_symbols)
    if missing or forbidden:
        print("Package distribution check failed:")
        for member in missing:
            print(f" - sdist missing required member: {member}")
        for label, matches in forbidden.items():
            print(f" - sdist contains forbidden {label}:")
            for member in matches:
                print(f"   - {member}")
        raise SystemExit(1)


def _assert_installed_wheel(wheel: Path, work_dir: Path) -> None:
    venv_dir = work_dir / "venv"
    _create_smoke_venv(venv_dir)
    python = _venv_python(venv_dir)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    _run([str(python), "-m", "pip", "install", str(wheel)], cwd=work_dir, env=env)

    contract_smoke = _run(
        [
            str(python),
            "-c",
            (
                "from semantic_rails.contracts import CONTRACT_NAMES, load_contract; "
                "assert all(load_contract(name) for name in CONTRACT_NAMES)"
            ),
        ],
        cwd=work_dir,
        env=env,
    )
    if contract_smoke.returncode != 0:  # pragma: no cover - _run raises first
        raise SystemExit(contract_smoke.returncode)

    packages = _run([str(python), "-m", "semantic_rails", "packages"], cwd=work_dir, env=env)
    package_payload = json.loads(packages.stdout)
    if "jaffle_shop" not in package_payload.get("packages", []):
        print("Package distribution check failed:")
        print(" - installed wheel did not register built-in jaffle_shop package")
        raise SystemExit(1)

    catalog = _run(
        [str(python), "-m", "semantic_rails", "catalog", "--package", "jaffle_shop"],
        cwd=work_dir,
        env=env,
    )
    catalog_payload = json.loads(catalog.stdout)
    package_id = (
        catalog_payload.get("catalog", {}).get("meta", {}).get("package", {}).get("package_id")
    )
    if package_id != "jaffle_shop":
        print("Package distribution check failed:")
        print(" - installed wheel catalog did not load jaffle_shop")
        raise SystemExit(1)

    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "order_count"}],
        "limit": 1,
    }
    result = _run(
        [
            str(python),
            "-m",
            "semantic_rails",
            "query",
            "--package",
            "jaffle_shop",
            "--query-json",
            json.dumps(query),
        ],
        cwd=work_dir,
        env=env,
    )
    result_payload = json.loads(result.stdout)
    if not result_payload.get("rows"):
        print("Package distribution check failed:")
        print(" - installed wheel query returned no rows for jaffle_shop")
        raise SystemExit(1)


def _verify_distribution(dist_dir: Path, work_dir: Path) -> tuple[Path, Path]:
    wheel, sdist = _release_artifacts(dist_dir)
    _assert_wheel_contents(wheel)
    _assert_sdist_contents(sdist)
    _assert_installed_wheel(wheel, work_dir)
    return wheel, sdist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="verify prebuilt artifacts without invoking uv build",
    )
    args = parser.parse_args(argv)
    if args.no_build and args.dist_dir is None:
        parser.error("--no-build requires --dist-dir")

    with tempfile.TemporaryDirectory(prefix="semantic-rails-dist-") as tmp:
        work_dir = Path(tmp)
        dist_dir = args.dist_dir.resolve() if args.dist_dir else work_dir / "dist"
        if not args.no_build:
            _build_distribution(dist_dir)
        wheel, sdist = _verify_distribution(dist_dir, work_dir)

    print(f"Package distribution check passed: {wheel.name}, {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
