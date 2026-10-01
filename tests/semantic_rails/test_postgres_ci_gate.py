"""Live Postgres correctness cannot be bypassed by the required CI aggregate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
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
    for name in ("HOST", "USER", "PASSWORD", "DATABASE"):
        assert job["env"][f"SR_POSTGRES_{name}"].strip()
    assert job["services"]["postgres"]["image"].startswith("postgres:16@sha256:")
    commands = [step.get("run") for step in job["steps"]]
    assert "uv sync --group dev --extra postgres --locked" in commands
    assert "uv run --no-sync pytest -q -rfE tests/semantic_rails/test_adbc_adapter.py" in commands
    execution = (
        "uv run --no-sync pytest -q -rfE tests/integration/correctness "
        "--junitxml=postgres-results.xml"
    )
    assert execution in commands
    guard = next(step for step in job["steps"] if step.get("name") == "Verify Postgres tests ran")
    assert job["steps"].index(guard) > commands.index(execution)


@pytest.mark.parametrize(
    "cases, succeeds",
    [
        ([], False),
        ([("duckdb", None)], False),
        ([("duckdb-postgres_compatibility", None)], False),
        ([("duckdb", None), ("postgres", "skipped")], False),
        ([("postgres", "pytest.xfail")], False),
        ([("postgres", "failure")], False),
        ([("postgres", "error")], False),
        ([("postgres", None)], True),
        ([("duckdb", None), ("postgres", None)], True),
        ([("postgres", None), ("postgres", "pytest.xfail")], True),
        ([("postgres", None), ("postgres", "skipped")], False),
        ([("postgres", None), ("duckdb", "skipped")], False),
    ],
)
def test_correctness_report_requires_completed_postgres_tests_without_skips(
    tmp_path, cases, succeeds
):
    suite = ET.Element("testsuite")
    for backend, outcome in cases:
        case = ET.SubElement(
            suite, "testcase", name=f"test_answer_matches_reference[{backend}-case]"
        )
        if outcome == "pytest.xfail":
            ET.SubElement(case, "skipped", type=outcome)
        elif outcome:
            ET.SubElement(case, outcome)
    ET.ElementTree(suite).write(tmp_path / "postgres-results.xml")
    result = run_correctness_report_guard(tmp_path)
    assert result.returncode == (0 if succeeds else 1), result.stdout + result.stderr
    if succeeds:
        assert "1 Postgres reference tests ran" in result.stdout


@pytest.mark.parametrize("report", [None, "invalid XML"])
def test_correctness_report_requires_readable_report(tmp_path, report):
    if report is not None:
        (tmp_path / "postgres-results.xml").write_text(report)
    result = run_correctness_report_guard(tmp_path)
    assert result.returncode != 0


def run_correctness_report_guard(directory):
    _, correctness = workflows()
    guard = next(
        step
        for step in correctness["jobs"]["postgres"]["steps"]
        if step.get("name") == "Verify Postgres tests ran"
    )
    script = guard["run"].split("uv run --no-sync python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=10,
    )


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
