#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from urllib.parse import unquote, urldefrag, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility for older system python shims.
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".claude",
    ".codex",
    ".git",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    ".uv-cache",
    ".pytest_cache",
    ".worktrees",
    "node_modules",
    "dist",
    "__pycache__",
}

STALE_LICENSE_PATTERNS = {
    "PolyForm": re.compile(r"\bPolyForm\b", re.IGNORECASE),
    "source-available": re.compile(r"\bsource[- ]available\b", re.IGNORECASE),
    "license-controlled": re.compile(r"\blicense[- ]controlled\b", re.IGNORECASE),
    "commercially licensable": re.compile(r"\bcommercially licensable\b", re.IGNORECASE),
    "commercial license": re.compile(r"\bcommercial license\b", re.IGNORECASE),
    "separate commercial": re.compile(r"\bseparate commercial\b", re.IGNORECASE),
    "larger businesses need": re.compile(r"\blarger businesses need\b", re.IGNORECASE),
    "eligible small-business": re.compile(r"\beligible small[- ]business\b", re.IGNORECASE),
}

POSTURE_SCAN_EXTENSIONS = {".html", ".md", ".txt", ".yml", ".yaml"}
POSTURE_SCAN_ROOTS = {
    ".github",
    "docs",
}
POSTURE_SCAN_FILES = {
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
}
POSTURE_SCAN_EXCLUDED_PREFIXES: set[str] = set()
PUBLIC_REFERENCE_ROOTS = (
    "README.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    "docs",
)
PUBLIC_REFERENCE_EXCLUDED_PREFIXES: set[str] = set()
PUBLIC_LINK_EXTENSIONS = {".md"}


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def project_version() -> str:
    text = read("pyproject.toml")
    if tomllib is not None:
        return str(tomllib.loads(text).get("project", {}).get("version", "") or "").strip()
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    return match.group(1).strip() if match else ""


def _walk_repo_files(excluded_dirs: set[str], *, exclude_egg_info: bool = False) -> list[str]:
    files: list[str] = []
    for root, dirs, filenames in os.walk(REPO_ROOT):
        dirs[:] = [
            dirname
            for dirname in dirs
            if dirname not in excluded_dirs
            and not (exclude_egg_info and dirname.endswith(".egg-info"))
        ]
        root_path = Path(root)
        for filename in filenames:
            path = root_path / filename
            files.append(path.relative_to(REPO_ROOT).as_posix())
    return files


def repo_files() -> list[str]:
    return _walk_repo_files(EXCLUDED_DIRS)


def public_reference_files() -> list[str]:
    files: list[str] = []
    for prefix in PUBLIC_REFERENCE_ROOTS:
        path = REPO_ROOT / prefix
        if path.is_file():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if not any(rel.startswith(excluded) for excluded in PUBLIC_REFERENCE_EXCLUDED_PREFIXES):
                files.append(rel)
            continue
        if not path.exists():
            continue
        for child in path.rglob("*"):
            rel = child.relative_to(REPO_ROOT).as_posix()
            if any(rel.startswith(excluded) for excluded in PUBLIC_REFERENCE_EXCLUDED_PREFIXES):
                continue
            if child.is_file() and child.suffix in PUBLIC_LINK_EXTENSIONS:
                files.append(rel)
    return sorted(set(files))


def posture_scan_files() -> list[str]:
    files: list[str] = []
    for rel in repo_files():
        path = Path(rel)
        if any(rel.startswith(prefix) for prefix in POSTURE_SCAN_EXCLUDED_PREFIXES):
            continue
        if rel in POSTURE_SCAN_FILES:
            files.append(rel)
            continue
        if (
            path.parts
            and path.parts[0] in POSTURE_SCAN_ROOTS
            and path.suffix in POSTURE_SCAN_EXTENSIONS
        ):
            files.append(rel)
    return files


def markdown_links(text: str) -> list[str]:
    return re.findall(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text)


def validate_local_links(errors: list[str]) -> None:
    for rel in public_reference_files():
        path = REPO_ROOT / rel
        text = path.read_text(encoding="utf-8")
        links = markdown_links(text)

        for raw_href in links:
            href = raw_href.strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https"}:
                continue
            if parsed.scheme:
                errors.append(f"{rel} has unsupported local link scheme: {href}")
                continue

            target_path, _fragment = urldefrag(href)
            if not target_path:
                continue
            if target_path.startswith("/"):
                errors.append(f"{rel} uses a site-root-relative link: {href}")
                continue
            else:
                target = (path.parent / unquote(target_path)).resolve()
            if not str(target).startswith(str(REPO_ROOT)):
                errors.append(f"{rel} links outside the repo: {href}")
                continue
            if not target.exists():
                errors.append(f"{rel} has broken local link: {href}")


def validate_public_references(errors: list[str]) -> None:
    forbidden_public_patterns = {
        # docs/internal/ was removed before launch; keep the pattern so a
        # public document cannot silently reintroduce the private prefix.
        "internal docs": re.compile(r"\bdocs/internal/|\binternal/CANON\.md\b"),
        "private repo": re.compile(r"\bsemantic-rails-cloud\b", re.IGNORECASE),
        "private absolute path": re.compile(r"/Users/(?:WTremml|will(?:\.tremml)?)", re.IGNORECASE),
        "private package/index marker": re.compile(r"\binternal-pypi\b", re.IGNORECASE),
        "private product client": re.compile(
            r"\bSemantic Rails Cloud\b|CLOUD_CHANGE_WORKFLOW\.md|semantic-rails cloud\b",
            re.IGNORECASE,
        ),
        "private hosted-demo policy": re.compile(
            r"\bSEMANTIC_RAILS_PUBLIC_DEMO\b|semantic_rails/public_demo\.py",
            re.IGNORECASE,
        ),
    }
    for rel in public_reference_files():
        text = read(rel)
        for label, pattern in forbidden_public_patterns.items():
            if pattern.search(text):
                errors.append(f"{rel} contains public {label} reference")


FORBIDDEN_PUBLIC_PATH_PREFIXES = ("website/", "deploy/cloudflare/")
FORBIDDEN_PUBLIC_PATHS = {
    ".github/workflows/deploy-cloudflare.yml",
    "docs/CLOUD_CHANGE_WORKFLOW.md",
    "scripts/dev/try_live_server.py",
    "scripts/smoke_public_demo.py",
    "semantic_rails/cloud_client.py",
    "semantic_rails/public_demo.py",
    "tests/semantic_rails/test_cloud_client.py",
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
    "private hosted-demo policy": re.compile(
        r"\bSEMANTIC_RAILS_PUBLIC_DEMO\b|\bpublic_demo_enabled\b|"
        r"\benforce_public_demo_(?:http_payload|mcp_message)\b"
    ),
}


def validate_public_boundary(errors: list[str]) -> None:
    files = repo_files()
    for rel in files:
        if rel in FORBIDDEN_PUBLIC_PATHS or rel.startswith(FORBIDDEN_PUBLIC_PATH_PREFIXES):
            errors.append(f"public boundary: private surface present: {rel}")
    for rel in files:
        path = Path(rel)
        if not rel.startswith("semantic_rails/") or path.suffix != ".py":
            continue
        text = read(rel)
        for label, pattern in FORBIDDEN_ENGINE_SYMBOL_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"public boundary: {rel} contains {label}")


REQUIRED_CODEOWNER_LINES = (
    "/semantic_rails/contracts/ @wtremml18",
    "/schemas/ @wtremml18",
    "/scripts/generate_contract_artifacts.py @wtremml18",
    "/scripts/check_contract_compatibility.py @wtremml18",
    "/docs/CONTRACTS.md @wtremml18",
    "/docs/QUERY_IR_SCHEMA.md @wtremml18",
    "/semantic_rails/embedding.py @wtremml18",
    "/semantic_rails/mcp*.py @wtremml18",
    "/semantic_rails/architect_*.py @wtremml18",
    "/pyproject.toml @wtremml18",
    "/semantic_rails/__init__.py @wtremml18",
    "/.github/workflows/ci.yml @wtremml18",
    "/.github/workflows/publish.yml @wtremml18",
    "/.github/workflows/release-readiness.yml @wtremml18",
    "/scripts/verify_package_distribution.py @wtremml18",
    "/scripts/verify_release_readiness.py @wtremml18",
    "/docs/RELEASE_CHECKLIST.md @wtremml18",
)


def validate_codeowners(errors: list[str]) -> None:
    codeowners = read(".github/CODEOWNERS")
    for required in REQUIRED_CODEOWNER_LINES:
        if required not in codeowners:
            errors.append(f".github/CODEOWNERS must include: {required}")


def validate_pyproject_metadata(errors: list[str]) -> None:
    pyproject_text = read("pyproject.toml")
    if tomllib is None:
        required_snippets = (
            'name = "semantic-rails"',
            'readme = "README.md"',
            'requires-python = ">=3.11"',
            'license = { file = "LICENSE" }',
            "authors = [",
            '"analytics"',
            '"semantic-layer"',
            '"sql"',
            'license = "Apache-2.0"',
            '"Programming Language :: Python :: 3.11"',
            '"Programming Language :: Python :: 3.12"',
            '"Topic :: Database"',
            "[project.urls]",
            'Homepage = "',
            'Documentation = "',
            'Source = "',
            'Issues = "',
            'Changelog = "',
        )
        for snippet in required_snippets:
            if snippet not in pyproject_text:
                errors.append(f"pyproject.toml is missing package metadata snippet: {snippet}")
        version = project_version()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.+-]+)?", version):
            errors.append(f"pyproject.toml has invalid project.version: {version!r}")
        return

    data = tomllib.loads(pyproject_text)
    project = data.get("project", {})
    required_strings = {
        "name": "semantic-rails",
        "readme": "README.md",
        "requires-python": ">=3.11",
    }
    for key, expected in required_strings.items():
        if project.get(key) != expected:
            errors.append(f"pyproject.toml project.{key} must be {expected!r}")
    version = str(project.get("version", "") or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.+-]+)?", version):
        errors.append(f"pyproject.toml has invalid project.version: {version!r}")

    # PEP 639: license is a SPDX string and license-files lists the bundled
    # license texts. We still accept the older table form (``{file = "LICENSE"}``)
    # for compatibility with tooling that has not migrated yet.
    license_value = project.get("license")
    license_files = project.get("license-files") or []
    if isinstance(license_value, str):
        license_ok = license_value.strip().upper() == "APACHE-2.0" and "LICENSE" in [
            str(entry) for entry in license_files
        ]
    elif isinstance(license_value, dict):
        license_ok = license_value.get("file") == "LICENSE"
    else:
        license_ok = False
    if "Apache License" not in read("LICENSE") or not license_ok:
        errors.append("pyproject.toml must point package metadata at the Apache-2.0 LICENSE file")
    if not project.get("authors"):
        errors.append("pyproject.toml must include public package authors")

    keywords = set(project.get("keywords", []))
    for keyword in ("analytics", "semantic-layer", "sql"):
        if keyword not in keywords:
            errors.append(f"pyproject.toml must include keyword: {keyword}")

    classifiers = set(project.get("classifiers", []))
    # PEP 639 dropped the ``License :: ...`` classifier in favour of the
    # SPDX ``license`` field; setuptools warns when both are present. We
    # check the license via the SPDX field above, so only the Python /
    # topic classifiers remain mandatory here.
    for classifier in (
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Database",
    ):
        if classifier not in classifiers:
            errors.append(f"pyproject.toml must include classifier: {classifier}")

    urls = project.get("urls", {})
    for label in ("Homepage", "Documentation", "Source", "Issues", "Changelog"):
        if not urls.get(label):
            errors.append(f"pyproject.toml must include project URL: {label}")


# Repo-hygiene scan: no committed files outside the public release surface,
# no internal-company strings in tracked content.
HYGIENE_ALLOWLIST = {
    "docs/ARCHITECTURE.md",
    "configs/examples/semantic_rails_capabilities_reference.yml",
    "configs/examples/semantic_rails_package_starter.yml",
    "scripts/verify_release_readiness.py",
}
HYGIENE_ALLOWLIST_SUFFIXES = {
    "docs/PACKAGE_AUTHORING.md",
    "docs/SNOWFLAKE_SHOWCASE_RUNBOOK.md",
    "tests/plan_candidate_envelope.py",
    "tests/semantic_rails/test_ergonomics_v2.py",
}
HYGIENE_GENERATED_PREFIXES = {
    "build/",
    "comparisons/semantic_layers/shared/results/",
}
HYGIENE_GENERATED_FILES = {
    "uv.lock",
}
HYGIENE_FORBIDDEN_CONTENT_PATTERNS = [
    re.compile(r"kla" r"viyo", re.IGNORECASE),
    re.compile(r"kl" r"_", re.IGNORECASE),
]
HYGIENE_FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"^build/"),
    re.compile(r"(^|/)[^/]+\.egg-info/"),
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.DS_Store$"),
    re.compile(r"^data/.+\.sqlite$"),
    re.compile(r"^data/.+\.duckdb$"),
    re.compile(r"^ma_ontology/"),
    re.compile(r"^jaffle-sl-template/"),
    re.compile(r"^requirements\.txt$"),
    re.compile(
        r"^configs/(?!semantic_rails/|examples/semantic_rails_capabilities_reference\.yml$"
        r"|examples/semantic_rails_package_starter\.yml$).+"
    ),
    # ``tests/mf2sr/`` is the test root for the MetricFlow → Semantic
    # Rails translator surfaced as ``semantic-rails import --from
    # metricflow`` (mf2sr package). ``tests/integration/`` is the
    # cross-warehouse conformance suite (targets, fixture loaders, and
    # the parity battery documented in docs/ADDING_A_DIALECT.md). Both
    # are part of the public release surface.
    re.compile(r"^tests/(?!semantic_rails/|mf2sr/|integration/|__init__\.py$|conftest\.py$).+"),
    re.compile(r"^contracts/"),
    re.compile(r"^capabilities/"),
]
HYGIENE_EXCLUDED_DIRS = EXCLUDED_DIRS | {"audit"}


def _hygiene_repo_files() -> list[str]:
    return _walk_repo_files(HYGIENE_EXCLUDED_DIRS, exclude_egg_info=True)


def validate_repo_hygiene(errors: list[str]) -> None:
    for path in _hygiene_repo_files():
        if path in HYGIENE_ALLOWLIST or path in HYGIENE_GENERATED_FILES:
            continue
        if any(path.startswith(prefix) for prefix in HYGIENE_GENERATED_PREFIXES):
            continue
        if path.startswith("data/") and path.endswith((".sqlite", ".duckdb")):
            continue
        if path in HYGIENE_ALLOWLIST_SUFFIXES:
            continue
        if any(pattern.search(path) for pattern in HYGIENE_FORBIDDEN_PATH_PATTERNS):
            errors.append(f"hygiene: forbidden file in repo: {path}")
            continue
        try:
            text = (REPO_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(text) for pattern in HYGIENE_FORBIDDEN_CONTENT_PATTERNS):
            errors.append(f"hygiene: forbidden content in {path}")


# Prelaunch gap checks: no private paths or stale license posture in shipped
# surfaces, and ASGI emits X-Request-ID.
PRIVATE_PATH_RE = re.compile(r"/Users/(?:WTremml|will(?:\.tremml)?)", re.IGNORECASE)
PRIVATE_PACKAGE_RE = re.compile(r"\binternal-pypi\b", re.IGNORECASE)
PRELAUNCH_STALE_LICENSE_RE = re.compile(
    r"\b(?:polyform|source[- ]available|license[- ]controlled|commercial license)\b",
    re.IGNORECASE,
)
PRELAUNCH_TEXT_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    ".uv-cache",
    ".pytest_cache",
    "node_modules",
    "__pycache__",
}


def _prelaunch_text_files(prefixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for prefix in prefixes:
        root = REPO_ROOT / prefix
        if root.is_file():
            files.append(root)
            continue
        for path in root.rglob("*"):
            if any(part in PRELAUNCH_TEXT_EXCLUDE_DIRS for part in path.parts):
                continue
            if path.is_file():
                files.append(path)
    return files


def _add_text_scan_errors(
    errors: list[str],
    *,
    prefixes: tuple[str, ...],
    pattern: re.Pattern[str],
    label: str,
) -> None:
    for path in _prelaunch_text_files(prefixes):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if pattern.search(text):
            errors.append(f"{label}: {path.relative_to(REPO_ROOT).as_posix()}")


def _validate_readme_opening(errors: list[str]) -> None:
    """The README is the PyPI long description: its opening must work for a
    pip-install user (self-contained inline payload, no repo files), with the
    clone/uv flow present as the contributor path right after."""
    lines = read("README.md").splitlines()
    first_25 = "\n".join(lines[:25])
    first_70 = "\n".join(lines[:70])
    if "agent-first semantic layer" not in first_25:
        errors.append("README opening must contain the one-sentence value prop")
    if "Apache-2.0" not in first_25:
        errors.append("README opening must contain the Apache-2.0 license callout")
    if not any(
        marker in first_70
        for marker in (
            "uv pip install semantic-rails",
            "uv pip install 'semantic-rails>=",
            "python -m pip install semantic-rails",
            "python -m pip install 'semantic-rails>=",
        )
    ):
        errors.append("README quickstart must include the published-package install path")
    if not any(
        command in first_70
        for command in ("semantic-rails setup --interactive", "semantic-rails ask ")
    ):
        errors.append("README quickstart must contain a runnable first command")
    if "@examples/" in first_70:
        errors.append("README pip quickstart must not depend on repo-only examples")
    if "@examples/jaffle_shop_revenue_by_store.json" not in read("README.md"):
        errors.append("README must keep the clone/uv contributor flow with the example file")


def validate_release_tag(errors: list[str], tag: str) -> None:
    if not tag:
        return
    expected = f"v{project_version()}"
    if tag != expected:
        errors.append(f"release tag {tag!r} must exactly match project version {expected!r}")


def validate_prelaunch_gaps(errors: list[str]) -> None:
    if (REPO_ROOT / "ui_demo").exists():
        errors.append("ui_demo/ workbench surface must not be present")
    if (REPO_ROOT / "semantic_rails/formulate").exists():
        errors.append("retired semantic_rails.formulate package must not ship")
    if (REPO_ROOT / "semantic_rails/planner/_legacy.py").exists():
        errors.append("retired formulate_payload adapter must not ship in semantic_rails")
    if (REPO_ROOT / "semantic_rails/metadata_parts/formulate.py").exists():
        errors.append("retired metadata_parts.formulate import shim must not ship")
    planner_text = "\n".join(
        read(rel)
        for rel in (
            "semantic_rails/planner/orchestrator.py",
            "semantic_rails/planner/patterns/_protocol.py",
            "semantic_rails/planner/patterns/inline_comparison.py",
            "semantic_rails/planner/patterns/metric_by_dimension_rollup.py",
        )
    )
    if "formulate_runtime_composition" in planner_text or "plan_only" in planner_text:
        errors.append("retired formulate planner split still appears in production code")
    for rel in (
        "CONTRIBUTING.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        "configs/examples/semantic_rails_capabilities_reference.yml",
    ):
        text = read(rel)
        if "ui_demo" in text or "workbench" in text.lower():
            errors.append(f"{rel} still documents the removed workbench surface")

    _validate_readme_opening(errors)

    if (REPO_ROOT / "uv.lock").exists():
        lock_text = read("uv.lock")
        if PRIVATE_PACKAGE_RE.search(lock_text):
            errors.append("uv.lock contains a private package/index marker")
        if PRIVATE_PATH_RE.search(lock_text):
            errors.append("uv.lock contains a private absolute path")

    _add_text_scan_errors(
        errors,
        prefixes=("README.md", "docs"),
        pattern=PRELAUNCH_STALE_LICENSE_RE,
        label="stale license wording",
    )
    _add_text_scan_errors(
        errors,
        prefixes=("README.md", "docs"),
        pattern=PRIVATE_PATH_RE,
        label="private path",
    )

    asgi = read("semantic_rails/asgi.py")
    if "x-request-id" not in asgi.lower():
        errors.append("ASGI app must emit X-Request-ID")


# V1 completion checks: required content in user-facing docs + structural
# validation of the active runtime package.
V1_RUNTIME_CONFIGS = [REPO_ROOT / "configs" / "semantic_rails" / "jaffle_shop"]


def _has_all(text: str, needles: list[str]) -> list[str]:
    return [needle for needle in needles if needle not in text]


def validate_v1_completion(errors: list[str]) -> None:
    from semantic_rails.config_validation import (
        read_text as _read_text,
    )
    from semantic_rails.config_validation import (
        validate_runtime_package,
    )

    readme = _read_text(REPO_ROOT / "README.md")
    capabilities = _read_text(REPO_ROOT / "docs" / "CAPABILITIES.md")
    docs_index = _read_text(REPO_ROOT / "docs" / "README.md")
    package_authoring = _read_text(REPO_ROOT / "docs" / "PACKAGE_AUTHORING.md")

    missing_readme = _has_all(
        readme,
        [
            "uv run semantic-rails packages",
            "docs/PACKAGE_AUTHORING.md",
            "docs/QUERY_API.md",
            "docs/CAPABILITIES.md",
            "parse-config",
            "validate-config",
            "configs/semantic_rails/jaffle_shop",
        ],
    )
    if missing_readme:
        errors.append(
            f"v1: README.md is missing required active-runtime references: {', '.join(missing_readme)}"
        )

    missing_runtime_refs = _has_all(
        capabilities + "\n" + docs_index,
        [
            "semantic_rails/",
            "configs/semantic_rails/jaffle_shop",
            "PACKAGE_AUTHORING.md",
            "QUERY_API.md",
            "CAPABILITIES.md",
            "parse-config",
            "validate-config",
        ],
    )
    if missing_runtime_refs:
        errors.append(
            "v1: canonical docs are missing required live-runtime references: "
            + ", ".join(missing_runtime_refs)
        )

    missing_package_authoring = _has_all(
        package_authoring,
        [
            "schema_version: 1",
            "graph",
            "models",
            "metrics",
            "name",
            "label",
            "joins",
            "accumulation",
            "parse-config",
            "validate-config",
        ],
    )
    if missing_package_authoring:
        errors.append(
            f"v1: docs/PACKAGE_AUTHORING.md is missing required authoring references: {', '.join(missing_package_authoring)}"
        )

    for path in V1_RUNTIME_CONFIGS:
        errors.extend(f"v1: {msg}" for msg in validate_runtime_package(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "Verify public release readiness.")
    parser.add_argument(
        "--tag",
        default="",
        help="Optional release tag; must exactly equal v<project.version>.",
    )
    args = parser.parse_args(argv)
    errors: list[str] = []

    required_files = [
        "LICENSE",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CODE_OF_CONDUCT.md",
        "README.md",
        "docs/AGENT_QUICKSTART.md",
        "docs/BENCHMARK_EVIDENCE.md",
        "docs/PACKAGE_AUTHORING.md",
        "docs/RELEASE_CHECKLIST.md",
        "docs/QUERY_API.md",
        "docs/CAPABILITIES.md",
        "docs/ARCHITECTURE.md",
        "docs/CONTRACTS.md",
        "configs/examples/semantic_rails_capabilities_reference.yml",
        "configs/examples/semantic_rails_package_starter.yml",
        "configs/semantic_rails/jaffle_shop/package.yml",
        "configs/semantic_rails/jaffle_shop/graph.yml",
        "scripts/verify_package_distribution.py",
        "scripts/verify_published_release.py",
        ".github/CODEOWNERS",
        ".github/workflows/ci.yml",
        ".github/workflows/publish.yml",
        ".github/workflows/release-readiness.yml",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/ISSUE_TEMPLATE/package_question.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
    ]
    for rel in required_files:
        if not (REPO_ROOT / rel).exists():
            errors.append(f"missing required file: {rel}")

    validate_local_links(errors)
    validate_public_references(errors)
    validate_public_boundary(errors)
    validate_codeowners(errors)
    validate_pyproject_metadata(errors)
    validate_repo_hygiene(errors)
    validate_prelaunch_gaps(errors)
    validate_v1_completion(errors)
    validate_release_tag(errors, args.tag)

    license_text = read("LICENSE")
    if (
        "Apache License" not in license_text
        or "Version 2.0, January 2004" not in license_text
        or "Licensed under the Apache License, Version 2.0" not in license_text
    ):
        errors.append("LICENSE must contain the Apache License 2.0 text")
    if "PolyForm" in license_text:
        errors.append("LICENSE still contains PolyForm text")

    pyproject = read("pyproject.toml")
    if 'name = "semantic-rails"' not in pyproject:
        errors.append("pyproject.toml must publish the semantic-rails project name")
    # The published wheel ships the core package plus mf2sr (the MetricFlow
    # translator behind `semantic-rails import`, gated by tests/mf2sr in
    # every workflow). Any other include list is rejected so internal-only
    # code (dev tooling, rename shims) cannot sneak into the published wheel.
    packages_find = (
        "[tool.setuptools.packages.find]" in pyproject
        and 'include = ["semantic_rails", "semantic_rails.*", "mf2sr", "mf2sr.*"]' in pyproject
    )
    if 'packages = ["semantic_rails"]' not in pyproject and not packages_find:
        errors.append("pyproject.toml must package semantic_rails and its subpackages only")
    if re.search(r"(?m)^ma-ontology\s*=", pyproject) or re.search(r"(?m)^rails\s*=", pyproject):
        errors.append("pyproject.toml still exposes legacy CLI aliases")

    readme = read("README.md")
    if "uv run semantic-rails packages" not in readme:
        errors.append("README.md must advertise semantic-rails as the canonical CLI")
    if "Apache" not in readme or not re.search(r"open[- ]source", readme, re.IGNORECASE):
        errors.append("README.md must describe the Apache-2.0 open-source license posture")
    for required_community_file in ("SUPPORT.md", "SECURITY.md", "CODE_OF_CONDUCT.md"):
        if required_community_file not in readme:
            errors.append(f"README.md must link to {required_community_file}")
    if "uv run rails packages" in readme:
        errors.append("README.md still references the legacy rails alias")
    for required_reference in (
        "docs/PACKAGE_AUTHORING.md",
        "docs/QUERY_API.md",
        "docs/CAPABILITIES.md",
        "docs/AGENT_QUICKSTART.md",
        "docs/BENCHMARK_EVIDENCE.md",
    ):
        if required_reference not in readme:
            errors.append(f"README.md must link to {required_reference}")
    wheel_name = f"semantic_rails-{project_version()}-py3-none-any.whl"
    for required_install_reference in (
        "uv build --out-dir dist",
        "scripts/verify_package_distribution.py --dist-dir dist --no-build",
    ):
        if required_install_reference not in readme:
            errors.append(
                f"README.md must document install/from-wheel flow with: {required_install_reference}"
            )
    wheel_install_references = (
        f"python -m pip install dist/{wheel_name}",
        f"uv pip install --python .venv/bin/python --reinstall dist/{wheel_name}",
    )
    if not any(reference in readme for reference in wheel_install_references):
        errors.append(
            "README.md must document install/from-wheel flow with one of: "
            + ", ".join(wheel_install_references)
        )
    if "alias_prefixes" in read("semantic_rails/http_core.py"):
        errors.append("HTTP capabilities must not advertise pre-release route aliases")
    for rel in (
        "README.md",
        "CONTRIBUTING.md",
        "configs/examples/semantic_rails_capabilities_reference.yml",
        "docs/AGENT_API_PATH.md",
        "docs/QUERY_API.md",
    ):
        text = read(rel)
        if (
            "compatibility aliases" in text
            or "root and `/api/*`" in text
            or "root paths, `/api/*`" in text
        ):
            errors.append(f"{rel} still documents pre-release HTTP route aliases")

    for rel in posture_scan_files():
        text = read(rel)
        for label, pattern in STALE_LICENSE_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{rel} still contains stale license posture wording: {label}")

    canonical_runtime_docs = read("docs/README.md") + "\n" + read("docs/CAPABILITIES.md")
    if "uv run rails packages" in canonical_runtime_docs:
        errors.append("canonical docs still reference the legacy rails alias")
    for required_reference in (
        "PACKAGE_AUTHORING.md",
        "QUERY_API.md",
        "CAPABILITIES.md",
    ):
        if required_reference not in canonical_runtime_docs:
            errors.append(f"canonical docs must reference {required_reference}")
    ci = read(".github/workflows/ci.yml")
    for required_ci_command in (
        "uv run pytest -q tests/semantic_rails tests/mf2sr -n auto",
        "uv run semantic-rails parse-config --package jaffle_shop",
        "uv run semantic-rails validate-config --package jaffle_shop --quiet",
        "uv run semantic-rails test-package --package jaffle_shop",
        "uv run python scripts/verify_release_readiness.py",
    ):
        if required_ci_command not in ci:
            errors.append(f".github/workflows/ci.yml must run: {required_ci_command}")

    release_readiness = read(".github/workflows/release-readiness.yml")
    for required_release_command in (
        "uv run python scripts/benchmark_plan.py --gate --output dist/agentic-governance-scorecard.json --markdown-output dist/agentic-governance-scorecard.md",
        "uv run semantic-rails check --package jaffle_shop --artifact dist/jaffle_shop.semantic-rails.tar.gz",
        "uv build --out-dir dist",
        "uv run python scripts/verify_package_distribution.py --dist-dir dist --no-build",
        "uv run python scripts/verify_release_readiness.py",
    ):
        if required_release_command not in release_readiness:
            errors.append(
                f".github/workflows/release-readiness.yml must run: {required_release_command}"
            )

    publish = read(".github/workflows/publish.yml")
    for required_publish_marker in (
        "uv build --out-dir dist",
        "scripts/verify_package_distribution.py --dist-dir dist --no-build",
        "scripts/verify_published_release.py",
        "cp release-assets/python/* schemas/*.json release-assets/github/",
        "sha256sum semantic_rails-* *.json > SHA256SUMS",
        "needs: post-publish",
        "gh release create",
        "release-assets/github/*.json",
        "release-assets/github/SHA256SUMS",
    ):
        if required_publish_marker not in publish:
            errors.append(
                f".github/workflows/publish.yml must preserve release marker: "
                f"{required_publish_marker}"
            )
    if publish.count("uv build") != 1:
        errors.append(".github/workflows/publish.yml must build release artifacts exactly once")

    legacy_patterns = [
        r"^ma_ontology/",
        r"^configs/packages/",
        r"^configs/ontology\.yml$",
        r"^configs/mappings\.yml$",
        r"^configs/mappings_missing_bridge\.yml$",
        r"^configs/mappings_missing_time\.yml$",
        r"^configs/jaffle_ontology\.yml$",
        r"^configs/jaffle_mappings\.yml$",
        r"^configs/banking_ontology\.yml$",
        r"^configs/banking_mappings\.yml$",
        r"^capabilities/",
        r"^contracts/",
        r"^jaffle-sl-template/",
        r"^requirements\.txt$",
    ]
    for rel in repo_files():
        if any(re.search(pattern, rel) for pattern in legacy_patterns):
            errors.append(f"legacy surface still present: {rel}")

    if errors:
        print("Release readiness check failed:")
        for error in errors:
            print(f" - {error}")
        return 1

    print("Release readiness check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
