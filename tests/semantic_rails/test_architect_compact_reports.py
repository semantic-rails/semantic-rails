"""Architect MCP responses are a compact summary unless the caller asks for the full report.

A write, ``validate_project`` and ``project_status`` used to echo the whole parse report: every
warning with its details and the whole route census. On a package with many measures or many
undecided join routes that is more than an MCP client shows, so the agent saw nothing. By default
they now answer with the status, the revision, warning counts by code, the first warnings, the
route census counts with its first undecided pairs, and what changed; ``full_report: true``
returns everything, as before.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from semantic_rails.architect_mcp import _summary, create_architect_mcp_server
from tests.semantic_rails.conftest import copy_package_config

FIRST_WARNINGS = 10
FIRST_UNDECIDED = 3


def _call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(server.call_tool(name, arguments))[1]


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, default=str))


def _ungoverned_measures(count: int) -> dict[str, Any]:
    """Measures without governance ``meta``: three parse warnings each."""
    return {
        f"amount_{index}": {
            "label": f"Amount {index}",
            "description": f"Amount number {index}.",
            "kind": "aggregate",
            "expr": f"amount + {index}",  # not one another's duplicates
            "value_type": "number",
        }
        for index in range(count)
    }


def test_a_write_answers_with_a_summary_unless_asked_for_the_full_report(tmp_path: Path) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _call(
        server,
        "create_project",
        {"package_id": "growth", "expected_revision": "absent", "idempotency_key": "create"},
    )
    write = {
        "project_path": created["project_path"],
        "model_id": "events",
        "entity_key": "event",
        "relation": "raw_events",
        "primary_key": ["event_id"],
        "measures": _ungoverned_measures(12),
        "expected_revision": created["revision"],
        "idempotency_key": "measures",
        "dry_run": True,
    }

    compact = _call(server, "upsert_model", write)
    full = _call(server, "upsert_model", {**write, "full_report": True})

    assert compact["ok"] is True and full["ok"] is True, compact
    assert compact["revision"] == full["revision"] == created["revision"]
    assert compact["proposed_revision"] == full["proposed_revision"]
    assert compact["changed_files"] == full["changed_files"] == ["models/core/events.yml"]
    # What changed: the diff, not the whole new file a second time.
    (change,) = compact["changes"]
    assert change["diff"] == full["changes"][0]["diff"] and "amount_11" in change["diff"]
    assert change["proposed_content"] is None
    assert "amount_11" in full["changes"][0]["proposed_content"]

    warnings = full["parse"]["warnings"]
    assert len(warnings) == 36 and all("details" in row for row in warnings)
    # A preview's messages name the project, not the temporary copy it was checked in.
    assert all(row["message"].startswith(created["project_path"]) for row in warnings)
    parse = compact["parse"]
    assert parse["ok"] is True and parse["summary"] == full["parse"]["summary"]
    assert parse["warning_counts"] == {"AUTHORING_PROFILE_WARNING": 36}
    assert parse["warnings"] == [
        {key: value for key, value in row.items() if key != "details"}
        for row in warnings[:FIRST_WARNINGS]
    ]
    assert parse["warnings_omitted"] == 36 - FIRST_WARNINGS
    assert "full_report" in compact["report"]
    assert _size(compact) * 2 < _size(full)


def test_status_and_validate_summarize_the_census_warnings_and_passing_checks(
    tmp_path: Path,
) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    server = create_architect_mcp_server(workspace_root=tmp_path)
    status = {"project_path": str(package)}

    compact = _call(server, "project_status", status)
    full = _call(server, "project_status", {**status, "full_report": True})

    undecided = full["route_census"]["undecided"]
    assert len(undecided) > FIRST_UNDECIDED
    census = compact["route_census"]
    assert census["counts"] == {name: len(rows) for name, rows in full["route_census"].items()}
    # The first pairs come whole, with the clarification options whose decisions record them.
    assert census["undecided"] == undecided[:FIRST_UNDECIDED]
    assert set(census) == {"counts", "undecided"}
    assert compact["revision"] == full["revision"]
    assert compact["parse"]["warning_counts"]["ROUTES_UNDECIDED"] == 1
    assert len(compact["parse"]["warnings"]) <= FIRST_WARNINGS
    assert _size(compact) < 20_000 < _size(full)

    runtime = {"project_path": str(package), "mode": "runtime"}
    compact_runtime = _call(server, "validate_project", runtime)
    full_runtime = _call(server, "validate_project", {**runtime, "full_report": True})
    assert compact_runtime["ok"] is True and full_runtime["ok"] is True
    assert compact_runtime["summary"] == full_runtime["summary"]
    assert len(full_runtime["probes"]) == full_runtime["summary"]["probes_total"]
    assert compact_runtime["probes"] == []  # only failing probes; every probe passed
    assert len(compact_runtime["parse"]["route_census"]["undecided"]) == FIRST_UNDECIDED
    assert _size(compact_runtime) < 20_000

    examples = _call(server, "validate_project", {"project_path": str(package), "mode": "examples"})
    assert examples["ok"] is True and examples["examples"] == []
    assert examples["summary"]["passed"] == examples["summary"]["examples_total"] > 0


def test_the_first_warnings_show_every_code_before_repeating_one() -> None:
    """A new kind of warning behind many of another kind still shows, in report order."""
    many = [{"code": "MANY", "message": f"many {index}", "details": {}} for index in range(15)]
    late = [{"code": "LATE", "message": "late", "details": {}}]
    rare = [{"code": "RARE", "message": "rare", "details": {}}]

    summary = _summary({"warnings": [*many[:12], *late, *many[12:], *rare]})

    assert [row["message"] for row in summary["warnings"]] == [
        *(f"many {index}" for index in range(8)),
        "late",
        "rare",
    ]
    assert summary["warning_counts"] == {"MANY": 15, "LATE": 1, "RARE": 1}
    assert summary["warnings_omitted"] == 7


def test_status_lists_package_files_not_internal_state(tmp_path: Path) -> None:
    """A package that is the workspace root holds the transaction receipts, one per write."""
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _call(
        server,
        "create_project",
        {"package_id": "growth", "expected_revision": "absent", "idempotency_key": "create"},
    )
    receipt = Path(created["project_path"]) / ".semantic-rails" / "receipt.json"
    receipt.parent.mkdir()
    receipt.write_text("{}", encoding="utf-8")
    status = {"project_path": created["project_path"]}

    compact = {row["path"] for row in _call(server, "project_status", status)["files"]}
    full = {
        row["path"]
        for row in _call(server, "project_status", {**status, "full_report": True})["files"]
    }

    assert full - compact == {".semantic-rails/receipt.json"}
    assert {"package.yml", ".gitignore"} <= compact
