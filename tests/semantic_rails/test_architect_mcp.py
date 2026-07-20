from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from semantic_rails.architect_mcp import (
    ARCHITECT_INTERFACE_VERSION,
    create_architect_mcp_server,
)


def _list_tools(server):
    return asyncio.run(server.list_tools())


def _call_tool(server, name: str, arguments: dict):
    _, structured = asyncio.run(server.call_tool(name, arguments))
    return structured


def _create_project(server, package_id: str, **arguments):
    return _call_tool(
        server,
        "create_project",
        {
            "package_id": package_id,
            "expected_revision": "absent",
            "idempotency_key": f"create-{package_id}",
            **arguments,
        },
    )


def _metric_spec(label: str) -> dict:
    return {
        "label": label,
        "description": f"{label} metric.",
        "kind": "aggregate",
        "measure": "total_amount",
        "value_type": "number",
        "meta": {
            "owner_team": "analytics",
            "review_priority": "low",
            "change_risk": "low",
        },
    }


def test_architect_mcp_registers_developer_project_tools(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)

    tool_names = {tool.name for tool in _list_tools(server)}

    assert {
        "architect_guidance",
        "setup_project_dialog",
        "create_project",
        "project_status",
        "upsert_model",
        "upsert_metric",
        "upsert_segment",
        "validate_project",
        "impact_project",
        "mcp_client_config",
    }.issubset(tool_names)
    assert ARCHITECT_INTERFACE_VERSION == "v1"

    mutation_names = {
        "archive_project_file",
        "create_project",
        "upsert_metric",
        "upsert_model",
        "upsert_segment",
        "write_project_file",
    }
    for tool in _list_tools(server):
        if tool.name not in mutation_names:
            continue
        assert {"expected_revision", "idempotency_key"}.issubset(set(tool.inputSchema["required"]))
        assert tool.outputSchema is not None
        assert {
            "base_revision",
            "changed_files",
            "changes",
            "current_revision",
            "expected_revision",
            "idempotency_key",
            "proposed_revision",
            "revision",
        }.issubset(set(tool.outputSchema["required"]))
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is True
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False


def test_setup_project_dialog_returns_noninteractive_draft(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)

    result = _call_tool(
        server,
        "setup_project_dialog",
        {"package_id": "Growth Core", "goal": "model product analytics"},
    )

    assert result["ok"] is True
    assert result["mode"] == "dialog_schema"
    assert result["recommended_next_tool"] == "create_project"
    assert result["draft_arguments"]["package_id"] == "growth_core"
    assert [question["id"] for question in result["questions"]] == [
        "package_id",
        "description",
        "first_entity",
        "relation",
        "primary_key",
        "time_column",
        "amount_column",
    ]


def test_create_project_scaffolds_parseable_and_runnable_package(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)

    created = _create_project(
        server,
        "growth_core",
        description="Growth analytics semantic package.",
        relation="Raw Events",
        primary_key="Event ID",
        time_column="Occurred At",
        amount_column="Net Amount",
    )
    project_path = tmp_path / "configs" / "semantic_rails" / "growth_core"

    assert created["ok"] is True
    assert created["project_path"] == str(project_path)
    assert created["parse"]["summary"]["entities"] == 1
    assert created["parse"]["summary"]["measures"] == 2
    assert created["parse"]["summary"]["metric_recipes"] == 2
    assert created["revision"].startswith("sha256:")
    assert (project_path / "package.yml").exists()
    assert (project_path / "graph.yml").exists()
    assert (project_path / "data" / "growth_core_csv" / "raw_events.csv").exists()

    runtime = _call_tool(
        server, "validate_project", {"project_path": str(project_path), "mode": "runtime"}
    )
    examples = _call_tool(
        server, "validate_project", {"project_path": str(project_path), "mode": "examples"}
    )
    package_tests = _call_tool(
        server, "validate_project", {"project_path": str(project_path), "mode": "tests"}
    )

    assert runtime["ok"] is True
    assert examples["ok"] is True
    assert package_tests["ok"] is True


def test_write_project_file_is_workspace_scoped(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _create_project(server, "scoped_core")
    project_path = tmp_path / "configs" / "semantic_rails" / "scoped_core"

    root_result = _call_tool(
        server,
        "write_project_file",
        {
            "project_path": str(tmp_path),
            "relative_path": "README.md",
            "content": "nope\n",
            "expected_revision": created["revision"],
            "idempotency_key": "root-write",
        },
    )
    result = _call_tool(
        server,
        "write_project_file",
        {
            "project_path": str(project_path),
            "relative_path": "../outside.yml",
            "content": "nope: true\n",
            "expected_revision": created["revision"],
            "idempotency_key": "outside-write",
        },
    )

    assert root_result["ok"] is False
    assert "package.yml" in root_result["error"]["message"]
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_CONFIG"


def test_create_project_sanitizes_seed_csv_path(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)

    result = _create_project(
        server,
        "safe_core",
        relation="../Unsafe Events",
    )
    project_path = tmp_path / "configs" / "semantic_rails" / "safe_core"

    assert result["ok"] is True
    assert (project_path / "data" / "safe_core_csv" / "unsafe_events.csv").exists()
    assert not (tmp_path / "configs" / "semantic_rails" / "Unsafe Events.csv").exists()


def test_upsert_metric_updates_project_and_revalidates_parse(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _create_project(server, "metric_core")
    project_path = tmp_path / "configs" / "semantic_rails" / "metric_core"

    result = _call_tool(
        server,
        "upsert_metric",
        {
            "project_path": str(project_path),
            "expected_revision": created["revision"],
            "idempotency_key": "upsert-double-amount",
            "metric_key": "metric_core.double_amount",
            "spec": {
                "as": "metric.metric_core.double_amount",
                "name": "metric_core.double_amount",
                "label": "Double amount",
                "description": "Twice the starter total amount.",
                "kind": "derived",
                "temporal_role": "temporal_role.metric_core_event_time",
                "value_type": "currency",
                "currency": "USD",
                "examples": ["How does double amount trend over time?"],
                "expression": {
                    "kind": "binary",
                    "op": "multiply",
                    "left": {"kind": "metric", "metric": "metric.metric_core.total_amount"},
                    "right": {"kind": "literal", "value": 2},
                },
            },
        },
    )

    assert result["ok"] is True
    assert result["parse"]["summary"]["metric_recipes"] == 3
    assert (project_path / "metrics" / "core" / "metric_core_double_amount.yml").exists()


def test_diff_project_rejects_compare_path_outside_workspace(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    _create_project(server, "current_core")
    project_path = tmp_path / "configs" / "semantic_rails" / "current_core"

    result = _call_tool(
        server,
        "diff_project",
        {
            "project_path": str(project_path),
            "compare_path": str(tmp_path.parent),
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_CONFIG"


def test_impact_project_missing_baseline_has_recovery_hint(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    _create_project(server, "impact_core")
    project_path = tmp_path / "configs" / "semantic_rails" / "impact_core"

    result = _call_tool(server, "impact_project", {"project_path": str(project_path)})

    assert result["ok"] is False
    assert "compare_path or base_ref" in result["error"]["message"]
    assert result["recovery_hints"]
    assert "compare_path" in result["recovery_hints"][0]["message"]


def test_mcp_client_config_uses_current_python_not_uv(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)

    result = _call_tool(server, "mcp_client_config", {"transport": "stdio"})

    assert result["ok"] is True
    assert result["command"][:3] == [sys.executable, "-m", "semantic_rails.architect_mcp"]
    assert result["stdio"]["command"] == sys.executable
    assert result["stdio"]["args"][:2] == ["-m", "semantic_rails.architect_mcp"]


def test_preview_reports_exact_changes_without_writing(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _create_project(server, "preview_core")
    project_path = Path(created["project_path"])
    before_revision = created["revision"]

    preview = _call_tool(
        server,
        "upsert_metric",
        {
            "project_path": str(project_path),
            "metric_key": "preview_metric",
            "spec": _metric_spec("Preview"),
            "expected_revision": before_revision,
            "idempotency_key": "preview-metric",
            "dry_run": True,
        },
    )

    assert preview["ok"] is True
    assert preview["status"] == "preview"
    assert preview["dry_run"] is True
    assert preview["revision"] == before_revision
    assert preview["proposed_revision"] != before_revision
    assert preview["changed_files"] == ["metrics/core/preview_metric.yml"]
    assert preview["changes"][0]["proposed_content"].startswith("metrics:\n")
    assert "preview_metric" in preview["changes"][0]["diff"]
    assert not (project_path / "metrics" / "core" / "preview_metric.yml").exists()

    status = _call_tool(server, "project_status", {"project_path": str(project_path)})
    assert status["revision"] == before_revision


def test_retry_replays_idempotently_and_key_reuse_with_new_intent_conflicts(
    tmp_path: Path,
):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _create_project(server, "idempotent_core")
    arguments = {
        "project_path": created["project_path"],
        "metric_key": "retry_metric",
        "spec": _metric_spec("Retry"),
        "expected_revision": created["revision"],
        "idempotency_key": "retry-metric-key",
    }

    first = _call_tool(server, "upsert_metric", arguments)
    replay = _call_tool(server, "upsert_metric", arguments)
    conflicting = _call_tool(
        server,
        "upsert_metric",
        {
            **arguments,
            "spec": _metric_spec("Different intent"),
        },
    )

    assert first["ok"] is True
    assert replay["ok"] is True
    assert replay["status"] == "replayed"
    assert replay["original_status"] == "upserted"
    assert replay["idempotent_replay"] is True
    assert replay["revision"] == first["revision"]
    assert conflicting["ok"] is False
    assert conflicting["error"]["code"] == "CONFIG_CONFLICT"
    assert conflicting["error"]["details"]["conflict_kind"] == "idempotency_key_reuse"


def test_two_writers_from_same_revision_cannot_lose_updates(tmp_path: Path):
    creator = create_architect_mcp_server(workspace_root=tmp_path)
    created = _create_project(creator, "race_core")
    project_path = Path(created["project_path"])
    base_revision = created["revision"]
    first_server = create_architect_mcp_server(workspace_root=tmp_path)
    second_server = create_architect_mcp_server(workspace_root=tmp_path)

    def write(server, key: str):
        return _call_tool(
            server,
            "upsert_metric",
            {
                "project_path": str(project_path),
                "metric_key": key,
                "spec": _metric_spec(key),
                "expected_revision": base_revision,
                "idempotency_key": f"race-{key}",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: write(*pair),
                [(first_server, "first_metric"), (second_server, "second_metric")],
            )
        )

    successful = [result for result in results if result["ok"]]
    conflicts = [result for result in results if not result["ok"]]
    assert len(successful) == 1
    assert len(conflicts) == 1
    assert conflicts[0]["error"]["code"] == "CONFIG_CONFLICT"
    assert conflicts[0]["error"]["details"]["conflict_kind"] == "stale_revision"
    assert conflicts[0]["error"]["details"]["expected_revision"] == base_revision
    existing = [
        (project_path / "metrics" / "core" / f"{key}.yml").exists()
        for key in ("first_metric", "second_metric")
    ]
    assert existing.count(True) == 1


def test_raw_write_parse_failure_rolls_back_bytes_and_revision(tmp_path: Path):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _create_project(server, "rollback_core")
    project_path = Path(created["project_path"])
    package_path = project_path / "package.yml"
    before = package_path.read_bytes()

    result = _call_tool(
        server,
        "write_project_file",
        {
            "project_path": str(project_path),
            "relative_path": "package.yml",
            "content": "schema_version: [\n",
            "expected_revision": created["revision"],
            "idempotency_key": "invalid-raw-write",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "rolled_back_after_parse_error"
    assert result["revision"] == created["revision"]
    assert result["rolled_back"] is True
    assert package_path.read_bytes() == before
    status = _call_tool(server, "project_status", {"project_path": str(project_path)})
    assert status["revision"] == created["revision"]
