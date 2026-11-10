"""A metric asked "by month" is a series on the clock its measures share, or plan holds it.

The packages are the two a new team starts from: the starter scaffold (``semantic-rails init``),
and a scaffold over a dbt warehouse with a dbt model imported beside it. Both write metrics that
declare no ``temporal_role`` of their own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_scaffold import (
    FirstModel,
    ProjectSpec,
    ProjectWarehouse,
    project_scaffold_files,
)
from semantic_rails.architect_transactions import project_revision
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_dbt_artifacts
from tests.semantic_rails.result_helpers import assert_plan_held

NOW = "2026-11-10"
STARTER_CLOCK = "temporal_role.my_package_event_occurred_at"
SHOP_CLOCK = "temporal_role.shop_order_ordered_at"


def _scaffold(root: Path, spec: ProjectSpec) -> Path:
    package = root / spec.package_id
    for relative, contents in project_scaffold_files(spec).items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    return package


@pytest.fixture(scope="module")
def starter(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """The package ``semantic-rails init my_package --yes`` writes."""
    package = _scaffold(tmp_path_factory.mktemp("starter"), ProjectSpec(package_id="my_package"))
    with closing(opened(Runtime.from_path(str(package)))) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def dbt_shop(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """A scaffold on a dbt warehouse's orders mart, with dbt's customers mart imported.

    Orders are dated by ``ordered_at``, customers by ``signed_up_on``; the package's own
    ``orders_per_customer`` ratio divides one by the other, so no one clock dates it.
    """
    root = tmp_path_factory.mktemp("dbt_shop")
    spec = ProjectSpec(
        package_id="shop",
        warehouse=ProjectWarehouse(data="external", default_db="data/warehouse.duckdb"),
        first_model=FirstModel(
            entity="order",
            relation="main_marts.fct_orders",
            primary_key="order_id",
            time_column="ordered_at",
            amount_column="order_total",
            dimension_column="status",
        ),
    )
    package = _scaffold(root, spec)
    db = build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    write_dbt_artifacts(db, root / "dbt" / "target")
    arguments = {
        "project_path": "shop",
        "target_dir": "dbt/target",
        "select": ["dim_customers"],
        "expected_revision": project_revision(package),
        "idempotency_key": "import",
    }

    async def run() -> dict[str, Any]:
        server = create_architect_mcp_server(workspace_root=root)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("import_dbt_project", arguments)
            return dict(result.structuredContent or {})

    imported = asyncio.run(run())
    assert imported["ok"] is True, imported
    metrics = package / "metrics" / "core.yml"
    document = yaml.safe_load(metrics.read_text(encoding="utf-8"))
    document["metrics"]["orders_per_customer"] = {
        "label": "Orders per customer",
        "kind": "ratio",
        "numerator": "order_count",
        "denominator": "customer_count",
        "value_type": "number",
    }
    metrics.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with closing(opened(Runtime.from_path(str(package)))) as runtime:
        yield runtime


def _series(runtime: Runtime, question: str, metric: str, clock: str, grain: str) -> dict:
    payload = plan_payload(runtime, intent=question, now=NOW)
    assert payload["status"] == "ok", payload
    assert "warnings" not in payload, payload
    query = payload["best"]["query"]
    assert query["select"] == [metric], query
    assert query["time"] == {"temporal_role": clock, "grain": grain}, query
    return query


@pytest.mark.parametrize(
    ("question", "metric", "grain"),
    [
        ("total amount by month", "metric.my_package.total_amount", "month"),
        ("total amount by week", "metric.my_package.total_amount", "week"),
        ("event count by day", "metric.my_package.event_count", "day"),
    ],
)
def test_a_starter_metric_by_a_grain_is_a_series(
    starter: Runtime, question: str, metric: str, grain: str
) -> None:
    _series(starter, question, metric, STARTER_CLOCK, grain)


def test_a_starter_metric_by_day_returns_a_row_per_day(starter: Runtime) -> None:
    query = _series(
        starter, "event count by day", "metric.my_package.event_count", STARTER_CLOCK, "day"
    )
    rows = starter.query(query, now=NOW)["rows"]
    assert [(row["period"], row["event_count"]) for row in rows] == [
        ("2026-01-01", 1),
        ("2026-01-02", 1),
    ]


@pytest.mark.parametrize(
    ("question", "grain"),
    [("order count by month", "month"), ("order count by week", "week")],
)
def test_a_dbt_package_metric_by_a_grain_is_a_series(
    dbt_shop: Runtime, question: str, grain: str
) -> None:
    _series(dbt_shop, question, "metric.shop.order_count", SHOP_CLOCK, grain)


def test_a_dbt_package_metric_by_month_returns_a_row_per_month(dbt_shop: Runtime) -> None:
    query = _series(
        dbt_shop, "order count by month", "metric.shop.order_count", SHOP_CLOCK, "month"
    )
    rows = dbt_shop.query(query, now=NOW)["rows"]
    assert [(row["period"], row["order_count"]) for row in rows] == [
        ("2024-01-01", 2),
        ("2024-02-01", 3),
        ("2024-03-01", 3),
    ]


def test_a_dbt_package_metric_by_day_is_a_series(dbt_shop: Runtime) -> None:
    _series(dbt_shop, "order count by day", "metric.shop.order_count", SHOP_CLOCK, "day")


def test_a_metric_no_one_clock_dates_is_held_by_month(dbt_shop: Runtime) -> None:
    payload = plan_payload(dbt_shop, intent="orders per customer by month", now=NOW)
    assert_plan_held(payload, "PLAN_UNMATCHED_TERMS")
    assert payload["best"]["query"] == {"select": ["metric.shop.orders_per_customer"]}
    why = payload["why"]
    assert why["message"] == (
        "The question asks for a series by month, but the draft has no time grain: no one "
        "clock dates all of what it selects. So plan doesn't call it ready."
    )
    assert why["details"] == {
        "terms": ["month"],
        "kind": "time_grain_unrealized",
        "temporal_roles": [],
    }
    assert [hint["kind"] for hint in why["recovery_hints"]] == ["choose_temporal_role"]
    # One total is still one total, and the caller's clock answers the series.
    assert plan_payload(dbt_shop, intent="orders per customer", now=NOW)["status"] == "ok"
    chosen = plan_payload(
        dbt_shop,
        intent="orders per customer by month",
        partial_query={
            "select": ["metric.shop.orders_per_customer"],
            "time": {"temporal_role": SHOP_CLOCK, "grain": "month"},
        },
        now=NOW,
    )
    assert chosen["status"] == "ok", chosen
    assert chosen["best"]["query"]["time"] == {"temporal_role": SHOP_CLOCK, "grain": "month"}


def test_a_metric_asked_for_one_total_keeps_no_time(starter: Runtime) -> None:
    payload = plan_payload(starter, intent="total amount", now=NOW)
    assert payload["status"] == "ok", payload
    assert payload["best"]["query"] == {"select": ["metric.my_package.total_amount"]}
