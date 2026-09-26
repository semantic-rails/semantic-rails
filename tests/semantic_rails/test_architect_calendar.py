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


def _monthly_orders(workspace: Path) -> tuple[list[tuple[Any, Any]], str]:
    """Orders per month from December 2023 to April 2024, filled, and the SQL that filled them."""
    engine = Runtime.from_path(str(workspace / "shop"))
    month = "temporal_role.shop_order_ordered_at__month"
    try:
        result = engine.query(
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
        )
    finally:
        engine.close()
    return [(row[month], row["orders"]) for row in result["rows"]], str(result["rendered_sql"])


def test_mcp_session_adds_the_package_calendar(workspace: Path) -> None:
    # Without a calendar the implicit Gregorian one fills; the authored one takes over below.
    implicit_rows, implicit_sql = _monthly_orders(workspace)
    assert "implicit_calendar" in implicit_sql
    server = create_architect_mcp_server(workspace_root=workspace)
    before = project_revision(workspace / "shop")
    arguments = {"project_path": "shop", **_calendar(), "expected_revision": before}

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            description = tools["upsert_model"].description or ""
            assert "calendar: true makes it the package calendar" in description

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
    rows, sql = _monthly_orders(workspace)
    assert "main_marts.dim_date" in sql and "implicit_calendar" not in sql
    assert rows == [
        (date(2023, 12, 1), 0),
        (date(2024, 1, 1), 2),
        (date(2024, 2, 1), 3),
        (date(2024, 3, 1), 3),
        (date(2024, 4, 1), 0),
    ]
    assert [(month.date(), orders) for month, orders in implicit_rows] == rows


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


def _fiscal_calendar(**extra: Any) -> dict[str, Any]:
    return _calendar(
        model_id="fiscal_calendar",
        entity_key="fiscal_date",
        relation="dim_fiscal",
        calendar_id="fiscal",
        **extra,
    )


def _add_fiscal_table(workspace: Path) -> None:
    """dim_fiscal in the default schema, with fiscal months starting on the 6th."""
    conn = duckdb.connect(str(workspace / "shop" / "data" / "warehouse.duckdb"))
    try:
        conn.execute(
            "CREATE TABLE dim_fiscal AS SELECT d::DATE AS date_day, "
            "date_trunc('week', d)::DATE AS week_start, "
            "(date_trunc('month', d - INTERVAL 5 DAY) + INTERVAL 5 DAY)::DATE AS month_start, "
            "date_trunc('quarter', d)::DATE AS quarter_start, "
            "date_trunc('year', d)::DATE AS year_start "
            "FROM range(DATE '2023-01-01', DATE '2025-01-01', INTERVAL 1 DAY) AS t(d)"
        )
    finally:
        conn.close()


def test_a_second_calendar_buckets_by_its_own_months(workspace: Path) -> None:
    _add_fiscal_table(workspace)
    project = _project(workspace)
    assert project.upsert_model(**_calendar()).report["ok"] is True
    assert project.upsert_model(**_fiscal_calendar()).report["ok"] is True
    month = "temporal_role.shop_order_ordered_at__month"
    engine = Runtime.from_path(str(workspace / "shop"))
    try:
        rows = engine.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": "measure.shop.order_count"}, "as": "orders"}],
                "time": {
                    "temporal_role": "temporal_role.shop_order_ordered_at",
                    "grain": "month",
                    "start": "2024-01-06 00:00:00",
                    "end": "2024-04-06 00:00:00",
                    "fill": True,
                    "calendar_id": "fiscal",
                },
                "order_by": [{"field": month}],
            }
        )["rows"]
    finally:
        engine.close()

    assert [(row[month], row["orders"]) for row in rows] == [
        (date(2024, 1, 6), 4),
        (date(2024, 2, 6), 2),
        (date(2024, 3, 6), 2),
    ]


def test_one_calendar_per_calendar_id(workspace: Path) -> None:
    project = _project(workspace)
    assert project.upsert_model(**_calendar()).report["ok"] is True
    assert project.upsert_model(**_calendar()).report["ok"] is True  # re-applying is fine
    fiscal = {"model_id": "fiscal_calendar", "entity_key": "fiscal_date"}

    with pytest.raises(SemanticLayerError, match="would belong to both 'date' and 'fiscal_date'"):
        project.upsert_model(**_calendar(**fiscal))

    mutation = project.upsert_model(**_calendar(**fiscal, calendar_id="fiscal"))
    assert mutation.report["ok"] is True, mutation.report
    assert _model(workspace, "fiscal_calendar")["calendar_id"] == "fiscal"
    assert _graph_entity(workspace, "fiscal_date")["kind"] == "time"


def test_a_package_with_calendars_needs_a_default_one(workspace: Path) -> None:
    project = _project(workspace)

    with pytest.raises(SemanticLayerError, match="needs a default one"):
        project.upsert_model(**_fiscal_calendar())
    assert project.upsert_model(**_calendar()).report["ok"] is True
    with pytest.raises(SemanticLayerError, match="needs a default one"):
        project.upsert_model(**_calendar(calendar_id="gregorian"))


@pytest.mark.parametrize("calendar_id", ["Fiscal", "fiscal year", "fiscal-2024"])
def test_calendar_ids_are_lowercase_tokens(workspace: Path, calendar_id: str) -> None:
    with pytest.raises(SemanticLayerError, match="lowercase letters, digits and underscores"):
        _project(workspace).upsert_model(**_calendar(calendar_id=calendar_id))


def test_a_batch_is_checked_as_a_whole(workspace: Path) -> None:
    project = _project(workspace)
    second = _calendar(model_id="days", entity_key="day")

    with pytest.raises(SemanticLayerError, match="would belong to both"):
        project.upsert_models([_calendar(), second], validate_after=False)
    assert not (workspace / "shop" / "models" / "core" / "calendar.yml").exists()

    assert project.upsert_models([_calendar(dimensions=None)]).report["ok"] is True
    moved = project.upsert_models(
        [_calendar(dimensions=None, calendar=False), {**second, "dimensions": None}]
    )
    assert moved.report["ok"] is True, moved.report
    assert "kind" not in _graph_entity(workspace, "date")
    assert _graph_entity(workspace, "day")["kind"] == "time"


def test_an_unrelated_update_leaves_the_calendar_as_it_is(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_model(**_calendar())
    entity = _graph_entity(workspace, "date")

    mutation = project.upsert_model(**_calendar(calendar=None, description="One row per day."))

    assert mutation.report["ok"] is True, mutation.report
    assert _graph_entity(workspace, "date") == entity
    assert _model(workspace, "calendar")["description"] == "One row per day."
    assert _model(workspace, "calendar")["calendar_id"] == "default"


def test_calendar_false_makes_a_calendar_a_regular_entity_again(workspace: Path) -> None:
    project = _project(workspace)
    created = project.upsert_model(**_calendar(dimensions=None))
    assert created.report["ok"] is True, created.report
    assert _graph_entity(workspace, "date")["kind"] == "time"

    mutation = project.upsert_model(**_calendar(dimensions=None, calendar=False))

    assert mutation.report["ok"] is True, mutation.report
    entity = _graph_entity(workspace, "date")
    assert "kind" not in entity and entity["allowed_as_root"] is True
    assert "calendar_id" not in _model(workspace, "calendar")


def test_calendar_false_is_rolled_back_while_date_dimensions_remain(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_model(**_calendar())
    before = project_revision(workspace / "shop")

    mutation = project.upsert_model(**_calendar(calendar=False))

    assert mutation.report["status"] == "rolled_back_after_parse_error"
    assert "non-time entity" in str(mutation.report["errors"])
    assert project_revision(workspace / "shop") == before


def test_a_regular_model_can_be_bound_to_a_calendar(workspace: Path) -> None:
    _add_fiscal_table(workspace)
    project = _project(workspace)
    project.upsert_model(**_calendar())
    project.upsert_model(**_fiscal_calendar())
    orders = {
        "model_id": "orders",
        "entity_key": "order",
        "relation": "main_marts.fct_orders",
        "primary_key": ["order_id"],
    }

    with pytest.raises(SemanticLayerError, match="which no calendar declares"):
        project.upsert_model(**orders, calendar_id="retail")
    bound = project.upsert_model(**orders, calendar_id="fiscal")
    kept = project.upsert_model(**orders, calendar=False)  # a regular model: nothing to undo

    assert bound.report["ok"] is True and kept.report["ok"] is True, (bound, kept)
    orders_model = yaml.safe_load((workspace / "shop" / "models" / "orders.yml").read_text())
    assert orders_model["model"]["calendar_id"] == "fiscal"
    assert "kind" not in _graph_entity(workspace, "order")


def test_retries_replay_and_stale_writers_conflict_before_calendar_checks(
    workspace: Path,
) -> None:
    project = _project(workspace)
    stale = project_revision(workspace / "shop")
    request = {**_calendar(), "expected_revision": stale, "idempotency_key": "calendar"}

    first = project.upsert_model(**request)
    again = project.upsert_model(**request)
    with pytest.raises(SemanticLayerError) as conflict:
        # Against today's package this second default calendar would be refused.
        project.upsert_model(
            **_calendar(model_id="days", entity_key="day"),
            expected_revision=stale,
            idempotency_key="days",
        )

    assert first.report["ok"] is True and again.report["status"] == "replayed"
    assert conflict.value.details["conflict_kind"] == "stale_revision"
