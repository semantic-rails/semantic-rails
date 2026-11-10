"""A metric asked "by month" is a series on the clock its measures share, or plan holds it.

The packages are the two a new team starts from: the starter scaffold (``semantic-rails init``),
and a scaffold over a dbt warehouse with a dbt model imported beside it. Both write metrics that
declare no ``temporal_role`` of their own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
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
from tests.semantic_rails.conftest import copy_package_config, opened
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
    # Choosing a clock dates the orders by it, but not the customers: the hint says so.
    assert why["recovery_hints"] == [
        {
            "kind": "choose_temporal_role",
            "message": _parts_hint("Order count: Ordered At; Customer Count: Signed Up On"),
        }
    ]
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


# A word the draft already spends is no grain: a filter value it keeps ("for monthly billing
# period"), or the name of what it selects ("Revenue per day", "Monthly active users"). Each
# package below is the starter with one edit; each row is (question, the value the plan
# answers, the time block it drafts). The value plan answered before is in the comment.

_PLAN_ROWS = (
    "event_id,occurred_at,event_type,amount\n"
    "1,2026-01-01T09:00:00,monthly,100.0\n"
    "2,2026-01-02T09:00:00,annual,75.5\n"
    "3,2026-02-03T09:00:00,monthly,40.0\n"
    "4,2026-03-04T09:00:00,weekly,10.0\n"
)
MONTH = {"temporal_role": STARTER_CLOCK, "grain": "month"}


def _edit_yaml(path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _billing_periods(package: Path) -> None:
    """The event type is a billing period (monthly, annual, weekly), with a measure beside it."""
    (package / "data" / "my_package_csv" / "raw_events.csv").write_text(_PLAN_ROWS)

    def edit(document: dict[str, Any]) -> None:
        model = document["model"]
        model["dimensions"]["event_type"].update(
            label="Billing period", domain=["monthly", "annual", "weekly"]
        )
        model["measures"]["gross_amount"] = {
            "label": "Gross amount",
            "kind": "aggregate",
            "expr": "amount",
            "default_agg": "sum",
            "accumulation": {"kind": "flow"},
            "value_type": "number",
        }

    _edit_yaml(package / "models" / "core" / "events.yml", edit)


def _rates(package: Path) -> None:
    """Metrics whose names carry a grain word, with and without a declared clock."""

    def edit(document: dict[str, Any]) -> None:
        ratio = {"kind": "ratio", "numerator": "total_amount", "denominator": "event_count"}
        document["metrics"].update(
            revenue_per_day={"label": "Revenue per day", **ratio, "value_type": "number"},
            revenue_per_day_declared={
                "label": "Average daily amount",
                **ratio,
                "value_type": "number",
                "temporal_role": STARTER_CLOCK,
            },
            monthly_recurring_revenue={
                "label": "Monthly recurring revenue",
                "kind": "aggregate",
                "measure": "total_amount",
                "value_type": "number",
            },
        )

    _edit_yaml(package / "metrics" / "core.yml", edit)


def _grain_labels(package: Path) -> None:
    """The starter's metrics labelled with a grain word: Monthly active users, Daily revenue."""

    def edit(document: dict[str, Any]) -> None:
        document["metrics"]["event_count"]["label"] = "Monthly active users"
        document["metrics"]["total_amount"]["label"] = "Daily revenue"

    _edit_yaml(package / "metrics" / "core.yml", edit)


def _undated_accounts(package: Path) -> None:
    """An undated accounts model with its own billing period, beside the dated events."""
    _billing_periods(package)
    (package / "data" / "my_package_csv" / "raw_accounts.csv").write_text(
        "account_id,billing_period,plan_amount\n1,monthly,30.0\n2,monthly,20.0\n3,annual,200.0\n"
    )
    (package / "models" / "core" / "accounts.yml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "id": "accounts",
                    "label": "Accounts",
                    "relation": "raw_accounts",
                    "entities": {"account": {}},
                    "dimensions": {
                        "billing_period": {
                            "label": "Billing period",
                            "kind": "categorical",
                            "domain": ["monthly", "annual", "weekly"],
                        }
                    },
                    "measures": {
                        "plan_amount": {
                            "label": "Plan amount",
                            "kind": "aggregate",
                            "expr": "plan_amount",
                            "default_agg": "sum",
                            "accumulation": {"kind": "flow"},
                            "value_type": "number",
                        }
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    def edit(document: dict[str, Any]) -> None:
        document["graph"]["entities"]["account"] = {
            "label": "Account",
            "key": ["account_id"],
            "model": "accounts",
            "allowed_as_root": True,
        }

    _edit_yaml(package / "graph.yml", edit)


_VARIANTS = {
    "plans": _billing_periods,
    "rates": _rates,
    "labels": _grain_labels,
    "accounts": _undated_accounts,
}


@pytest.fixture(scope="module")
def variants(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Runtime]]:
    """Each starter variant, by name."""
    runtimes: dict[str, Runtime] = {}
    for name, edit in _VARIANTS.items():
        package = _scaffold(tmp_path_factory.mktemp(name), ProjectSpec(package_id="my_package"))
        edit(package)
        runtimes[name] = opened(Runtime.from_path(str(package)))
    yield runtimes
    for runtime in runtimes.values():
        runtime.close()


BILLING = "dimension.my_package_event_event_type"
_SPENT_WORDS = [
    # P: plan's answer before -> now.
    ("plans", "total amount for monthly billing period", [140.0], None),  # 100.0, 40.0 by month
    ("plans", "total amount where billing period is monthly", [140.0], None),  # by month
    ("plans", "event count for weekly billing", [1], None),  # by week
    ("plans", "gross amount for monthly billing period", [140.0], None),  # by month (a measure)
    ("plans", "total amount for annual billing period", [75.5], None),  # unchanged
    ("plans", "total amount for monthly billing period by month", [100.0, 40.0], MONTH),
    ("rates", "revenue per day by month", [87.75], MONTH),  # 100.0, 75.5 by day
    ("rates", "revenue per day", [87.75], None),  # 100.0, 75.5 by day
    ("rates", "monthly recurring revenue", [175.5], None),  # 175.5 for 2026-01
    ("rates", "average daily amount", [87.75], None),  # by day (declares its clock)
    ("rates", "average daily amount by month", [87.75], MONTH),  # by day
    ("labels", "monthly active users", [2], None),  # unchanged
    ("labels", "monthly active users by week", [2], {**MONTH, "grain": "week"}),
    ("labels", "daily revenue by month", [175.5], MONTH),  # unchanged
    ("accounts", "plan amount where billing period is monthly", [50.0], None),  # held
]


@pytest.mark.parametrize(
    ("package", "question", "values", "time"),
    _SPENT_WORDS,
    ids=[row[1] for row in _SPENT_WORDS],
)
def test_a_word_the_draft_spends_asks_for_no_grain(
    variants: dict[str, Runtime],
    package: str,
    question: str,
    values: list[float],
    time: dict[str, str] | None,
) -> None:
    runtime = variants[package]
    payload = plan_payload(runtime, intent=question, now=NOW)
    assert payload["status"] == "ok", payload
    query = payload["best"]["query"]
    assert query.get("time") == time, query
    rows = runtime.query(query, now=NOW)["rows"]
    value = query["select"][0].rsplit(".", 1)[-1]
    assert [row[value] for row in rows] == values, rows


def test_a_filter_value_keeps_its_rows_without_a_grain(
    variants: dict[str, Runtime],
) -> None:
    runtime = variants["plans"]
    grouped = plan_payload(
        runtime, intent="total amount by billing period for monthly and annual", now=NOW
    )
    assert grouped["status"] == "ok", grouped
    query = grouped["best"]["query"]
    assert "time" not in query, query
    assert query["where"] == [{"field": BILLING, "op": "is", "value": ["annual", "monthly"]}]
    rows = runtime.query(query, now=NOW)["rows"]
    assert sorted((row[BILLING], row["total_amount"]) for row in rows) == [
        ("annual", 75.5),
        ("monthly", 140.0),
    ]
    # Before, the month grain made this a ranking of (event, month) rows, held as ambiguous.
    ranked = plan_payload(
        runtime, intent="top 3 events by total amount for monthly billing period", now=NOW
    )
    assert ranked["status"] == "ok", ranked
    assert "time" not in ranked["best"]["query"], ranked["best"]["query"]
    rows = runtime.query(ranked["best"]["query"], now=NOW)["rows"]
    assert [row["total_amount"] for row in rows] == [100.0, 40.0]


@pytest.fixture(scope="module")
def jaffle_clocks(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """jaffle_shop with metrics that declare no clock over measures on different clocks.

    Two lifecycle measures are each read on the ordered and delivered clocks, with different
    defaults; the adoption funnel declares no clock.
    """
    package = copy_package_config(tmp_path_factory.mktemp("clocks"), "jaffle_shop")

    def lifecycle(document: dict[str, Any]) -> None:
        count = {"kind": "entity_count", "entity_key": "order_id", "value_type": "count"}
        document["model"]["measures"].update(
            lifecycle_orders_any={
                "label": "Lifecycle orders any clock",
                **count,
                "accumulation": {"kind": "event"},
                "times": ["ordered_at", "delivered_at"],
            },
            delivered_orders_any={
                "label": "Delivered orders any clock",
                **count,
                "accumulation": {"kind": "event"},
                "times": ["delivered_at", "ordered_at"],
            },
        )

    def funnel(document: dict[str, Any]) -> None:
        document["metrics"]["adoption.signup_to_send_conversion_rate_28d"].pop("temporal_role")

    _edit_yaml(package / "models" / "extensions" / "order_lifecycle.yml", lifecycle)
    _edit_yaml(package / "metrics" / "extensions" / "advanced_metrics.yml", funnel)

    def total(measure: str, aggregation: str = "sum") -> dict[str, str]:
        part = {"kind": "aggregate", "measure": measure, "aggregation": aggregation}
        return {key: value for key, value in part.items() if value}

    def minus(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "arithmetic", "op": "subtract", "left": left, "right": right}

    gap = minus(total("measure.jaffle.revenue_usd"), total("measure.jaffle.delivered_revenue_usd"))
    metrics = {
        "lifecycle.delivery_ratio": {
            "label": "Delivery ratio",
            "kind": "ratio",
            "numerator": "delivered_orders_any",
            "denominator": "lifecycle_orders_any",
            "value_type": "number",
        },
        "sales.undelivered_revenue_gap": {
            "label": "Undelivered revenue gap",
            "kind": "derived",
            "value_type": "currency",
            "expression": gap,
        },
        "sales.adjusted_gap": {
            "label": "Adjusted gap",
            "kind": "derived",
            "value_type": "currency",
            "expression": minus(gap, total("measure.jaffle.session_starts", "")),
        },
    }
    (package / "metrics" / "extensions" / "clock_parts.yml").write_text(
        yaml.safe_dump({"metrics": metrics}, sort_keys=False), encoding="utf-8"
    )
    with closing(opened(Runtime.from_path(str(package)))) as runtime:
        yield runtime


_SET_ROLE = (
    "Set query.time.temporal_role to the date the user means (details.temporal_roles, or a "
    "time role inspect lists for what the draft selects) and query.time.grain to month, then "
    "validate; or ask the user which date they mean, or whether one total answers the question."
)


def _parts_hint(parts: str) -> str:
    return (
        f"Its parts are dated by different clocks ({parts}). Setting query.time.temporal_role "
        "doesn't re-date a part onto a clock it isn't read on: that part keeps its own clock and "
        "only the periods line up, so each period would combine numbers dated differently. Ask "
        "the user which they mean, or use a metric declared on one clock."
    )


_HOLDS = [
    (
        "signup to send adoption funnel (28d) by month",
        "it isn't grouped by Session started at",
        ["temporal_role.jaffle_session_started_at"],
        _SET_ROLE,
    ),
    (
        "delivery ratio by month",
        "what it selects can be read on several clocks (Lifecycle delivered at, Lifecycle "
        "ordered at) and names none",
        [
            "temporal_role.jaffle_lifecycle_delivered_at",
            "temporal_role.jaffle_lifecycle_ordered_at",
        ],
        _SET_ROLE,
    ),
    (
        "undelivered revenue gap by month",
        "no one clock dates all of what it selects",
        [],
        _parts_hint("Revenue: Order time; Delivered revenue: Lifecycle delivered at"),
    ),
    # Three clocks: the first two share none, so none dates the whole. Before, plan drafted it
    # on the session clock, which dates one part of three.
    (
        "adjusted gap by month",
        "no one clock dates all of what it selects",
        [],
        _parts_hint(
            "Revenue: Order time; Delivered revenue: Lifecycle delivered at; Session starts: "
            "Session started at"
        ),
    ),
]


@pytest.mark.parametrize(
    ("question", "reason", "clocks", "hint"), _HOLDS, ids=[r[0] for r in _HOLDS]
)
def test_each_hold_names_why_no_clock_answers_the_series(
    jaffle_clocks: Runtime, question: str, reason: str, clocks: list[str], hint: str
) -> None:
    payload = plan_payload(jaffle_clocks, intent=question, now=NOW)
    assert_plan_held(payload, "PLAN_UNMATCHED_TERMS")
    assert "time" not in payload["best"]["query"], payload["best"]["query"]
    why = payload["why"]
    assert why["message"] == (
        f"The question asks for a series by month, but the draft has no time grain: {reason}. "
        "So plan doesn't call it ready."
    )
    assert why["details"] == {
        "terms": ["month"],
        "kind": "time_grain_unrealized",
        "temporal_roles": clocks,
    }
    assert why["recovery_hints"] == [{"kind": "choose_temporal_role", "message": hint}]
