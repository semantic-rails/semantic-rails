"""Live Postgres correctness cannot be bypassed by the required CI aggregate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflows():
    directory = ROOT / ".github/workflows"
    return (
        yaml.safe_load((directory / "ci.yml").read_text()),
        yaml.safe_load((directory / "postgres-correctness.yml").read_text()),
    )


def test_ci_calls_correctness_once_without_path_filter_or_independent_triggers():
    ci, correctness = workflows()
    assert set(ci.get("on", ci.get(True))) == {
        "push",
        "pull_request",
        "merge_group",
        "workflow_dispatch",
    }
    assert set(correctness.get("on", correctness.get(True))) == {"workflow_call"}
    callers = [
        (name, job)
        for name, job in ci["jobs"].items()
        if job.get("uses") == "./.github/workflows/postgres-correctness.yml"
    ]
    assert callers == [("postgres", {"uses": "./.github/workflows/postgres-correctness.yml"})]
    assert "concurrency" not in correctness  # Parent CI owns cancellation.
    assert set(correctness["jobs"]) == {"postgres"}
    job = correctness["jobs"]["postgres"]
    assert job["runs-on"] == "ubuntu-latest"
    assert "if" not in job and not job.get("continue-on-error")
    assert all("if" not in step and not step.get("continue-on-error") for step in job["steps"])
    gate = ci["jobs"]["all-checks"]
    assert "postgres" in gate["needs"]
    assert gate["if"] == "${{ always() }}"


def test_correctness_requires_strict_fixtures_locked_extra_and_execution_steps():
    _, correctness = workflows()
    job = correctness["jobs"]["postgres"]
    assert job["env"]["SR_INTEGRATION_STRICT"] == "1"
    assert job["services"]["postgres"]["image"].startswith("postgres:16@sha256:")
    commands = [step.get("run") for step in job["steps"]]
    assert "uv sync --group dev --extra postgres --locked" in commands
    assert "uv run --no-sync pytest -q -rfE tests/integration/correctness" in commands
    # Once the bound-filter integration suite is present, its live step must
    # remain in this same required job, alongside differential correctness.
    if (ROOT / "tests/integration/test_adbc_postgres.py").exists():
        assert (
            "uv run --no-sync pytest -q -rfE tests/integration/test_conformance.py "
            "tests/integration/test_adbc_postgres.py -k postgres"
        ) in commands


@pytest.mark.parametrize("event", ["pull_request", "push", "merge_group", "workflow_dispatch"])
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", "missing"])
def test_aggregate_requires_correctness_success(event, result):
    ci, _ = workflows()
    gate = ci["jobs"]["all-checks"]
    steps = gate["steps"]
    assert len(steps) == 1
    step = steps[0]
    assert not step.get("continue-on-error")
    assert step["env"]["RESULTS"] == "${{ toJSON(needs) }}"
    assert step["env"]["EVENT"] == "${{ github.event_name }}"
    # Execute the actual aggregate's embedded Python, rather than a copy of
    # its decision logic. Docs-only PRs may skip other path-filtered jobs.
    script = step["run"].split("python3 - <<'EOF'\n", 1)[1].rsplit("\nEOF", 1)[0]
    needs = {name: {"result": "success"} for name in gate["needs"]}
    if event == "pull_request":
        for name in ("backend", "lint", "security"):
            needs[name]["result"] = "skipped"
    if result == "missing":
        needs.pop("postgres", None)
    else:
        needs["postgres"] = {"result": result}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "RESULTS": json.dumps(needs), "EVENT": event},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == (0 if result == "success" else 1), completed.stderr
    if result != "success":
        assert "postgres" in completed.stderr
