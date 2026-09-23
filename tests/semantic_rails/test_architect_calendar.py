"""The package calendar through upsert_model: a kind: time entity that time.fill reads."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import project_revision
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

GRAINS = ("week_start", "month_start", "quarter_start", "year_start")


def _calendar(**extra: Any) -> dict[str, Any]:
    """upsert_model arguments for a calendar over main_marts.dim_date."""
    return {
        "model_id": "calendar",
        "entity_key": "date",
        "relation": "main_marts.dim_date",
        "primary_key": ["date_day"],
        "times": {
            "date_day": {
                "label": "Calendar day",
                "column": "date_day",
                "kind": "date",
                "class": "calendar_time",
            }
        },
        "dimensions": {
            grain: {"label": grain.replace("_", " ").title(), "kind": "date"} for grain in GRAINS
        },
        "calendar": True,
        **extra,
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """The orders package over the dbt warehouse, plus a dim_date the calendar can read."""
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    db_path = build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE main_marts.dim_date AS SELECT d::DATE AS date_day, "
            "date_trunc('week', d)::DATE AS week_start, "
            "date_trunc('month', d)::DATE AS month_start, "
            "date_trunc('quarter', d)::DATE AS quarter_start, "
            "date_trunc('year', d)::DATE AS year_start "
            "FROM range(DATE '2023-01-01', DATE '2025-01-01', INTERVAL 1 DAY) AS t(d)"
        )
    finally:
        conn.close()
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _graph_entity(workspace: Path, key: str) -> dict[str, Any]:
    graph = yaml.safe_load((workspace / "shop" / "graph.yml").read_text(encoding="utf-8"))
    return dict(graph["graph"]["entities"][key])


def _model(workspace: Path, model_id: str) -> dict[str, Any]:
    path = workspace / "shop" / "models" / "core" / f"{model_id}.yml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["model"])


def _monthly_orders(workspace: Path) -> list[tuple[Any, Any]]:
    """Orders per month from December 2023 to April 2024, filled from the calendar."""
    engine = Runtime.from_path(str(workspace / "shop"))
    month = "temporal_role.shop_order_ordered_at__month"
    try:
        rows = engine.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": "measure.shop.order_count"}, "as": "orders"}],
                "time": {
                    "temporal_role": "temporal_role.shop_order_ordered_at",
                    "grain": "month",
                    "start": "2023-12-01 00:00:00",
                    "end": "2024-05-01 00:00:00",
                    "fill": True,
                },
                "order_by": [{"field": month}],
            }
        )["rows"]
    finally:
        engine.close()
    return [(row[month], row["orders"]) for row in rows]


def test_mcp_session_adds_the_package_calendar(workspace: Path) -> None:
    with pytest.raises(SemanticLayerError, match="requires a calendar entity"):
        _monthly_orders(workspace)
    server = create_architect_mcp_server(workspace_root=workspace)
    before = project_revision(workspace / "shop")
    arguments = {"project_path": "shop", **_calendar(), "expected_revision": before}

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert "calendar" in (tools["upsert_model"].description or "")

            async def call(name: str, **values: Any) -> dict[str, Any]:
                return dict((await session.call_tool(name, values)).structuredContent or {})

            preview = await call("upsert_model", **arguments, idempotency_key="p", dry_run=True)
            applied = await call("upsert_model", **arguments, idempotency_key="a")
            runtime = await call("validate_project", project_path="shop", mode="runtime")
            return [preview, applied, runtime]

    preview, applied, runtime = asyncio.run(run())

    assert preview["ok"] is True and preview["dry_run"] is True
    assert {change["path"] for change in preview["changes"]} == {
        "graph.yml",
        "models/core/calendar.yml",
    }
    assert applied["ok"] is True, applied
    assert runtime["ok"] is True, runtime
    entity = _graph_entity(workspace, "date")
    assert (entity["kind"], entity["allowed_as_root"], entity["model"]) == (
        "time",
        False,
        "calendar",
    )
    assert _model(workspace, "calendar")["calendar_id"] == "default"
    assert _monthly_orders(workspace) == [
        (date(2023, 12, 1), 0),
        (date(2024, 1, 1), 2),
        (date(2024, 2, 1), 3),
        (date(2024, 3, 1), 3),
        (date(2024, 4, 1), 0),
    ]


def test_date_dimensions_on_a_regular_entity_are_rolled_back(workspace: Path) -> None:
    before = project_revision(workspace / "shop")

    mutation = _project(workspace).upsert_model(**_calendar(calendar=None))

    assert mutation.report["ok"] is False
    assert "non-time entity" in str(mutation.report)
    assert project_revision(workspace / "shop") == before


def test_dry_run_apply_and_undo(workspace: Path) -> None:
    project = _project(workspace)
    before = project_revision(workspace / "shop")

    preview = project.upsert_model(**_calendar(), dry_run=True)
    assert preview.report["ok"] is True, preview.report
    assert project_revision(workspace / "shop") == before

    applied = project.upsert_model(**_calendar())
    assert applied.report["ok"] is True, applied.report
    assert applied.report["revision"] == preview.report["proposed_revision"]

    assert applied.undo()["ok"] is True
    assert project_revision(workspace / "shop") == before


def test_one_calendar_per_calendar_id(workspace: Path) -> None:
    project = _project(workspace)
    assert project.upsert_model(**_calendar()).report["ok"] is True
    fiscal = {"model_id": "fiscal_calendar", "entity_key": "fiscal_date"}

    with pytest.raises(SemanticLayerError, match="already belongs to calendar entity 'date'"):
        project.upsert_model(**_calendar(**fiscal))

    mutation = project.upsert_model(**_calendar(**fiscal, calendar_id="fiscal"))
    assert mutation.report["ok"] is True, mutation.report
    assert _model(workspace, "fiscal_calendar")["calendar_id"] == "fiscal"
    assert _graph_entity(workspace, "fiscal_date")["kind"] == "time"


def test_calendars_import_in_a_batch(workspace: Path) -> None:
    mutation = _project(workspace).upsert_models([_calendar()])

    assert mutation.report["ok"] is True, mutation.report
    assert _graph_entity(workspace, "date")["kind"] == "time"


def test_an_unrelated_update_leaves_the_calendar_as_it_is(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_model(**_calendar())
    entity = _graph_entity(workspace, "date")

    mutation = project.upsert_model(**_calendar(calendar=None, description="One row per day."))

    assert mutation.report["ok"] is True, mutation.report
    assert _graph_entity(workspace, "date") == entity
    assert _model(workspace, "calendar")["description"] == "One row per day."
    assert _model(workspace, "calendar")["calendar_id"] == "default"


def test_calendar_false_makes_it_a_regular_entity_again(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_model(**_calendar(dimensions=None))

    mutation = project.upsert_model(**_calendar(dimensions=None, calendar=False))

    assert mutation.report["ok"] is True, mutation.report
    entity = _graph_entity(workspace, "date")
    assert "kind" not in entity and entity["allowed_as_root"] is True
    assert "calendar_id" not in _model(workspace, "calendar")


def test_calendar_id_needs_a_calendar(workspace: Path) -> None:
    with pytest.raises(SemanticLayerError, match="pass calendar: true"):
        _project(workspace).upsert_model(**_calendar(calendar=None, calendar_id="fiscal"))
