"""Release-mechanics regression contract.

Audit findings on packaging/release hygiene, pinned here so they cannot
silently regress:

- ``semantic_rails/py.typed`` (PEP 561) must exist and be declared as
  package data so type checkers consume the wheel's inline annotations.
- ``jsonschema`` must stay in the dev dependency group — without it the
  schema-contract tests skip silently instead of running.
- ``make release-check`` must route through ``uv run`` (bare ``python3``
  cannot import ``semantic_rails`` outside the project venv).
- The publish workflow must use PyPI Trusted Publishing (OIDC) with a
  ``pypi`` environment, and third-party actions in workflows must be
  SHA-pinned (a tag like ``@v4`` is mutable and a supply-chain risk).
- docker-compose must bind the unauthenticated API to loopback by
  default, and the Dockerfile/compose healthchecks must probe the same
  endpoint.
- Classifiers must match positioning: Beta (not Alpha), and the CI
  matrix / classifiers must both cover Python 3.14.

These tests read repo files rather than executing builds so they stay
fast enough for the default test run.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
MAKEFILE = REPO_ROOT / "Makefile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-readiness.yml"
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"

SHA_PINNED = re.compile(r"@[0-9a-f]{40}(\s|$)")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _workflow_action_uses(path: Path) -> list[str]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    uses: list[str] = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if "uses" in step:
                uses.append(step["uses"])
    return uses


def test_py_typed_marker_exists_and_is_declared_package_data():
    marker = REPO_ROOT / "semantic_rails" / "py.typed"
    assert marker.exists(), (
        "semantic_rails/py.typed is missing — without the PEP 561 marker, "
        "type checkers ignore the package's inline annotations."
    )
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert "py.typed" in package_data.get("semantic_rails", []), (
        "py.typed must be listed under [tool.setuptools.package-data] "
        "semantic_rails, otherwise it is silently dropped from the wheel."
    )


def test_jsonschema_is_a_dev_dependency():
    dev = _pyproject()["dependency-groups"]["dev"]
    assert any(dep.startswith("jsonschema") for dep in dev), (
        "jsonschema must stay in the dev group — the schema-contract tests "
        "(test_query_ir_schema.py and friends) skip silently without it."
    )


def test_jsonschema_actually_imports_so_schema_tests_run():
    # Belt and braces: the skip in test_query_ir_schema.py is silent, so
    # assert importability here where a missing dep fails loudly.
    import jsonschema  # noqa: F401


def test_make_release_check_routes_through_uv():
    makefile = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^release-check:\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert match, "Makefile must define a release-check target"
    recipe = match.group(1)
    assert "uv run" in recipe, (
        "release-check must run via `uv run` — bare python3 cannot import "
        "semantic_rails outside the project venv."
    )
    assert not re.search(r"(?<![\w/])python3 ", recipe), (
        "release-check must not invoke bare python3 directly."
    )


def test_publish_workflow_uses_trusted_publishing():
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as boolean True.
    trigger = workflow.get("on", workflow.get(True))
    tags = trigger["push"]["tags"]
    assert "v*" in tags, "publish workflow must trigger on v* tags"

    jobs = workflow["jobs"]
    publish_jobs = [
        job
        for job in jobs.values()
        if any("gh-action-pypi-publish" in step.get("uses", "") for step in job.get("steps", []))
    ]
    assert publish_jobs, "publish workflow must use pypa/gh-action-pypi-publish"
    (publish_job,) = publish_jobs
    assert publish_job.get("environment") == "pypi", (
        "publish job must run in the `pypi` environment so Trusted "
        "Publishing and any required-reviewer gate apply."
    )
    assert publish_job.get("permissions", {}).get("id-token") == "write", (
        "publish job needs id-token: write for the OIDC token exchange."
    )
    assert publish_job.get("needs") == "test" or "test" in (publish_job.get("needs") or []), (
        "publish job must depend on the test job so the suite runs first."
    )


def test_third_party_actions_are_sha_pinned():
    offenders: list[str] = []
    for path in (CI_WORKFLOW, PUBLISH_WORKFLOW, RELEASE_WORKFLOW):
        for uses in _workflow_action_uses(path):
            if not SHA_PINNED.search(uses + " "):
                offenders.append(f"{path.name}: {uses}")
    assert not offenders, (
        "Third-party actions must be SHA-pinned (mutable tags are a "
        f"supply-chain risk): {offenders}"
    )


def test_ci_pins_dorny_paths_filter_to_a_commit_sha():
    uses = [u for u in _workflow_action_uses(CI_WORKFLOW) if u.startswith("dorny/paths-filter")]
    assert uses, "ci.yml is expected to use dorny/paths-filter"
    for entry in uses:
        assert SHA_PINNED.search(entry + " "), (
            f"dorny/paths-filter must be SHA-pinned, found: {entry}"
        )


def test_compose_binds_api_to_loopback_by_default():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    ports = compose["services"]["semantic-rails"]["ports"]
    for mapping in ports:
        assert str(mapping).startswith("127.0.0.1:"), (
            "docker-compose must bind the unauthenticated API to loopback "
            f"by default, found: {mapping!r}"
        )


def test_compose_documents_auth_and_cors_env_vars():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "SEMANTIC_RAILS_API_KEYS" in text and "SEMANTIC_RAILS_CORS_ORIGINS" in text, (
        "docker-compose.yml must point non-local deployments at the "
        "SEMANTIC_RAILS_API_KEYS / SEMANTIC_RAILS_CORS_ORIGINS env vars."
    )


def test_docker_healthchecks_probe_the_same_endpoint():
    endpoint = re.compile(r"/api/v1/(\w+)")
    dockerfile_routes = set(endpoint.findall(DOCKERFILE.read_text(encoding="utf-8")))
    compose_text = " ".join(
        str(part)
        for part in yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][
            "semantic-rails"
        ]["healthcheck"]["test"]
    )
    compose_routes = set(endpoint.findall(compose_text))
    assert dockerfile_routes == compose_routes == {"ready"}, (
        "Dockerfile and docker-compose healthchecks must probe the same "
        f"endpoint (/api/v1/ready); got {dockerfile_routes} vs {compose_routes}"
    )


def test_dockerfile_base_image_is_precisely_pinned():
    match = re.search(r"^FROM\s+(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "Dockerfile must have a FROM line"
    image = match.group(1)
    assert re.match(r"python:3\.\d+\.\d+-slim", image), (
        "Base image must pin an exact patch release (e.g. python:3.12.13-"
        f"slim-trixie), not a floating tag; found: {image}"
    )


def test_classifiers_match_positioning():
    classifiers = _pyproject()["project"]["classifiers"]
    assert "Development Status :: 4 - Beta" in classifiers
    assert not any("3 - Alpha" in c for c in classifiers), (
        "Positioning is production-grade; Alpha undersells it."
    )
    assert "Programming Language :: Python :: 3.14" in classifiers


def test_ci_matrix_covers_python_314():
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["backend"]["strategy"]["matrix"]["python-version"]
    assert "3.14" in matrix, "CI backend matrix must exercise Python 3.14"


def test_publish_job_restates_contents_read_permission():
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    (publish_job,) = [
        job
        for job in jobs.values()
        if any("gh-action-pypi-publish" in step.get("uses", "") for step in job.get("steps", []))
    ]
    perms = publish_job.get("permissions", {})
    # Job-level permissions REPLACE the workflow defaults, so the publish
    # job that runs actions/checkout must restate contents: read or the
    # checkout step fails before the wheel is ever built.
    assert perms.get("contents") == "read", (
        "publish job sets its own permissions for id-token, which drops the "
        "workflow-level contents:read — restate it so checkout can read the repo."
    )


def test_release_readiness_workflow_collects_agentic_governance_scorecard():
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "release-readiness.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["release-readiness"]
    run_commands = [step.get("run", "") for step in job["steps"]]
    assert any(
        "uv run python scripts/benchmark_plan.py --gate" in command
        and "--output dist/agentic-governance-scorecard.json" in command
        and "--markdown-output dist/agentic-governance-scorecard.md" in command
        for command in run_commands
    ), "release-readiness must publish the agentic governance benchmark scorecard"

    upload_steps = [
        step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact")
    ]
    assert upload_steps, "release-readiness must upload release evidence artifacts"
    upload_path = str(upload_steps[-1]["with"]["path"])
    assert "dist/agentic-governance-scorecard.*" in upload_path


def test_publish_runs_complete_gate_and_checks_tag_version():
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    for command in (
        "scripts/benchmark_plan.py --gate",
        "semantic-rails check --package jaffle_shop",
        "scripts/verify_package_distribution.py",
        'scripts/verify_release_readiness.py --tag "${GITHUB_REF_NAME}"',
    ):
        assert command in text
    assert "uv build --out-dir dist" in text
    assert "scripts/verify_package_distribution.py --dist-dir dist --no-build" in text


def test_publish_builds_once_and_transfers_exact_artifacts_through_post_publish():
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    all_runs = [
        str(step.get("run", ""))
        for job in jobs.values()
        for step in list(job.get("steps", []) or [])
    ]
    assert sum("uv build" in command for command in all_runs) == 1

    publish = jobs["publish"]
    assert publish["needs"] == "test"
    assert any(
        step.get("uses", "").startswith("actions/download-artifact@") for step in publish["steps"]
    )
    assert not any("uv build" in str(step.get("run", "")) for step in publish["steps"])

    post_publish = jobs["post-publish"]
    assert post_publish["needs"] == "publish"
    assert any(
        "scripts/verify_published_release.py" in str(step.get("run", ""))
        for step in post_publish["steps"]
    )

    release = jobs["release"]
    assert release["needs"] == "post-publish"
    assert release["permissions"]["contents"] == "write"
    release_commands = "\n".join(str(step.get("run", "")) for step in release["steps"])
    assert "sha256sum --check SHA256SUMS" in release_commands
    assert "gh release create" in release_commands
    assert "release-assets/github/*.whl" in release_commands
    assert "release-assets/github/*.tar.gz" in release_commands
    assert "release-assets/github/*.json" in release_commands
    assert "release-assets/github/SHA256SUMS" in release_commands


def test_post_publish_verifier_rejects_every_unexpected_release_file(monkeypatch):
    from scripts import verify_published_release as verifier

    expected = {
        "semantic_rails-0.2.0-py3-none-any.whl": "wheel-digest",
        "semantic_rails-0.2.0.tar.gz": "sdist-digest",
    }
    monkeypatch.setattr(
        verifier,
        "_published_artifacts",
        lambda _version: {
            **expected,
            "semantic_rails-0.2.0-cp312-cp312-manylinux.whl": "unexpected-digest",
        },
    )

    with pytest.raises(RuntimeError, match="unexpected=.*manylinux"):
        verifier._wait_for_exact_publish(expected, "0.2.0", attempts=1, delay=0)


def test_release_readiness_derives_version_and_rejects_mismatched_tag():
    from scripts.verify_release_readiness import project_version, validate_release_tag

    assert project_version() == _pyproject()["project"]["version"] == "0.2.0"
    errors: list[str] = []
    validate_release_tag(errors, "v9.9.9")
    assert errors and "v0.2.0" in errors[0]
    errors.clear()
    validate_release_tag(errors, "v0.2.0")
    assert errors == []


def test_public_release_is_not_coupled_to_private_site_deployment():
    assert not (REPO_ROOT / ".github" / "workflows" / "deploy-cloudflare.yml").exists()
    assert not (REPO_ROOT / "semantic_rails" / "public_demo.py").exists()
    publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8").lower()
    assert "cloudflare" not in publish
    assert "website/" not in publish


def test_distribution_boundary_gate_rejects_private_paths_and_symbols():
    from scripts.verify_package_distribution import (
        _forbidden_engine_symbols,
        _forbidden_members,
    )

    forbidden_paths = _forbidden_members(
        {
            "semantic_rails-0.2.0/website/index.html",
            "semantic_rails-0.2.0/semantic_rails/cloud_client.py",
            "semantic_rails-0.2.0/semantic_rails/public_demo.py",
        }
    )
    assert {
        "private website root",
        "private product client",
        "private hosted-demo policy",
    }.issubset(forbidden_paths)

    source = b"class CloudClient:\n    pass\n"
    forbidden_symbols = _forbidden_engine_symbols(
        {"semantic_rails/cloud_client_boundary.py"},
        lambda _name: source,
    )
    assert forbidden_symbols["private client type"] == ["semantic_rails/cloud_client_boundary.py"]


def test_node_dependency_audits_are_ci_gates_and_unsafe_cube_graph_is_not_shipped():
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "uv export --quiet --format requirements-txt --all-extras" in ci
    assert "deploy/cloudflare" not in ci
    assert "npm audit --prefix comparisons/semantic_layers/malloy --audit-level=high" in ci
    cube = REPO_ROOT / "comparisons" / "semantic_layers" / "cube"
    assert not (cube / "package.json").exists()
    assert not (cube / "package-lock.json").exists()
    for evidence_file in (
        "original-package.json.evidence",
        "original-package-lock.json.evidence",
        "npm-audit.json.evidence",
        "sbom.cdx.json.evidence",
        "evidence-manifest.json",
        "scripts/verify_evidence.py",
    ):
        assert (cube / evidence_file).is_file()
    assert "python3 comparisons/semantic_layers/cube/scripts/verify_evidence.py" in ci
    snapshot = (cube / "runtime-snapshot.json").read_text(encoding="utf-8")
    assert '"status": "captured-evidence-only"' in snapshot
    for evidence in (
        "security_audit_date",
        "node_version",
        "package_lock_sha256",
        "CycloneDX",
        "component_count",
        "manifest_sha256",
    ):
        assert evidence in snapshot


def test_contract_and_release_boundaries_have_codeowners():
    text = CODEOWNERS.read_text(encoding="utf-8")
    for path in (
        "/semantic_rails/contracts/",
        "/schemas/",
        "/scripts/generate_contract_artifacts.py",
        "/scripts/check_contract_compatibility.py",
        "/docs/CONTRACTS.md",
        "/docs/QUERY_IR_SCHEMA.md",
        "/semantic_rails/embedding.py",
        "/semantic_rails/mcp*.py",
        "/semantic_rails/architect_*.py",
        "/pyproject.toml",
        "/semantic_rails/__init__.py",
        "/.github/workflows/ci.yml",
        "/.github/workflows/publish.yml",
        "/.github/workflows/release-readiness.yml",
        "/scripts/verify_package_distribution.py",
        "/scripts/verify_release_readiness.py",
        "/docs/RELEASE_CHECKLIST.md",
    ):
        assert f"{path} @wtremml18" in text


def test_init_starter_template_ships_as_a_data_file():
    data_files = _pyproject()["tool"]["setuptools"]["data-files"]
    examples = data_files.get("share/semantic-rails/configs/examples", [])
    assert "configs/examples/semantic_rails_package_starter.yml" in examples, (
        "`semantic-rails init` reads the starter template at runtime via "
        "resolve_repo_path; it must ship as a data file or init breaks from "
        "an installed wheel (the file is not next to the importable package)."
    )


def test_init_produces_a_loadable_single_file_package(tmp_path):
    import argparse

    from semantic_rails.cli import cmd_init
    from semantic_rails.config import load_package_config

    target = tmp_path / "myshop"
    cmd_init(
        argparse.Namespace(output=str(target), package_id="myshop", namespace=None, force=False)
    )
    package_yml = target / "package.yml"
    assert package_yml.is_file(), "init must write package.yml"
    assert (target / "data" / "seed_example.sql").is_file()

    config = load_package_config(str(package_yml))
    assert config.package.package_id == "myshop"
    assert config.entities, "the starter package must declare entities"
    assert config.measures, "the starter package must declare measures"
