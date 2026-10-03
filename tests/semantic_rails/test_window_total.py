"""A time window with no grain returns one total; an oversized result is refused.

Gold values come from raw SQL against the seeded tables, not from the compiler.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.compiler import plan_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.mcp import (
    MCP_DEFAULT_MAX_RESULT_CHARS,
    SemanticLayerMCPAdapter,
)
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MetricConfig

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
REVENUE = {"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}
ORDERS = {"as": "orders", "expression": {"measure": "measure.jaffle.order_count"}}
LIMIT_ENV = "SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS"


@pytest.fixture()
def runtime(runtime_factory: Any) -> Iterator[Runtime]:
    rt = runtime_factory("jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


def _query(select: list[dict[str, Any]], time: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"version": 2, "select": select, "time": {"temporal_role": ORDER_TIME, **time}, **extra}


def _gold(runtime: Runtime, sql: str) -> list[dict[str, Any]]:
    """Run raw SQL on the runtime's own database, bypassing the compiler."""
    return list(runtime._get_adapter().query(sql))


def _codes(response: dict[str, Any]) -> list[str]:
    return [str(item.get("code")) for item in response.get("warnings") or []]


def test_a_window_without_a_grain_is_one_total(runtime: Runtime) -> None:
    response = runtime.query(
        _query([REVENUE, ORDERS], {"start": "2017-04-01", "end": "2017-07-01"})
    )
    gold = _gold(
        runtime,
        "SELECT COUNT(*) AS orders, SUM(order_total_cents) / 100.0 AS revenue FROM jaffle_order "
        "WHERE ordered_at >= TIMESTAMP '2017-04-01' AND ordered_at < TIMESTAMP '2017-07-01'",
    )
    assert response["row_count"] == 1
    row = response["rows"][0]
    assert set(row) == {"revenue", "orders"}  # no time column
    assert row["orders"] == gold[0]["orders"]
    assert row["revenue"] == pytest.approx(gold[0]["revenue"])
    assert [column["field"] for column in response["output_columns"]] == ["revenue", "orders"]
    assert len(response["assumptions"]) == 1 and "one total" in response["assumptions"][0]
    assert "UNGRAINED_TIME_PROJECTION" not in _codes(response)


def test_a_window_inside_one_day_is_one_total(runtime: Runtime) -> None:
    response = runtime.query(
        _query([REVENUE, ORDERS], {"start": "2017-04-03T12:00:00", "end": "2017-04-03T13:00:00"})
    )
    gold = _gold(
        runtime,
        "SELECT COUNT(*) AS orders, SUM(order_total_cents) / 100.0 AS revenue FROM jaffle_order "
        "WHERE ordered_at >= TIMESTAMP '2017-04-03 12:00:00' "
        "AND ordered_at < TIMESTAMP '2017-04-03 13:00:00'",
    )
    assert gold[0]["orders"] > 1  # a raw-timestamp answer would have been several rows
    assert response["row_count"] == 1
    assert response["rows"][0]["orders"] == gold[0]["orders"]
    assert response["rows"][0]["revenue"] == pytest.approx(gold[0]["revenue"])


def test_a_grouped_window_is_one_total_per_group(runtime: Runtime) -> None:
    response = runtime.query(
        _query(
            [REVENUE],
            {"start": "2017-04-01", "end": "2017-07-01"},
            group_by=[STORE],
            order_by=[{"field": "time"}, {"field": STORE}],
        )
    )
    gold = _gold(
        runtime,
        "SELECT s.store_name AS store, SUM(o.order_total_cents) / 100.0 AS revenue "
        "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
        "WHERE o.ordered_at >= TIMESTAMP '2017-04-01' AND o.ordered_at < TIMESTAMP '2017-07-01' "
        "GROUP BY 1",
    )
    got = {row[STORE]: row["revenue"] for row in response["rows"]}
    assert got == pytest.approx({row["store"]: row["revenue"] for row in gold})
    assert all(set(row) == {STORE, "revenue"} for row in response["rows"])


def test_a_grain_still_returns_one_row_per_period(runtime: Runtime) -> None:
    response = runtime.query(
        _query([REVENUE], {"grain": "month", "start": "2017-04-01", "end": "2017-07-01"})
    )
    gold = _gold(
        runtime,
        "SELECT DATE_TRUNC('month', ordered_at) AS month, SUM(order_total_cents) / 100.0 AS revenue "
        "FROM jaffle_order WHERE ordered_at >= TIMESTAMP '2017-04-01' "
        "AND ordered_at < TIMESTAMP '2017-07-01' GROUP BY 1",
    )
    key = f"{ORDER_TIME}__month"
    assert {row[key]: row["revenue"] for row in response["rows"]} == pytest.approx(
        {row["month"].isoformat(): row["revenue"] for row in gold}
    )
    assert response["assumptions"] == []


@pytest.mark.parametrize(
    "time",
    [{}, {"start": "2017-04-01"}, {"end": "2017-04-01"}],
    ids=["no bounds", "start only", "end only"],
)
def test_only_a_bounded_window_collapses(runtime: Runtime, time: dict[str, Any]) -> None:
    response = runtime.query(_query([REVENUE], time))
    collapses = bool(time)
    assert (response["row_count"] == 1) is collapses
    assert (ORDER_TIME not in response["rows"][0]) is collapses
    assert ("UNGRAINED_TIME_PROJECTION" in _codes(response)) is not collapses


def test_an_expression_that_needs_the_time_axis_is_not_collapsed(runtime: Runtime) -> None:
    def collapses(select: dict[str, Any]) -> bool:
        payload = _query([select], {"end": "2017-04-04"})
        return bool(plan_query(runtime._config, None, payload).time.get("window_total"))

    rolling = {"expression": {"metric": "metric.sales.rolling_7d_revenue_direct"}, "as": "r"}
    assert collapses(REVENUE)
    assert not collapses(rolling)

    # A cumulative runs: it keeps its time column and the warning, and says nothing about a total.
    cumulative = {"expression": {"metric": "metric.sales.cumulative_revenue"}, "as": "running"}
    response = runtime.query(_query([cumulative], {"end": "2017-04-04"}))
    gold = _gold(
        runtime,
        "SELECT SUM(order_total_cents) / 100.0 AS revenue FROM jaffle_order "
        "WHERE ordered_at < TIMESTAMP '2017-04-04'",
    )
    assert response["row_count"] > 1
    assert all(set(row) == {ORDER_TIME, "running"} for row in response["rows"])
    assert max(row["running"] for row in response["rows"]) == pytest.approx(gold[0]["revenue"])
    assert "UNGRAINED_TIME_PROJECTION" in _codes(response)
    assert response["assumptions"] == []


# The predicate measures orders on the query's own clock (order time).
CUSTOMER_PREDICATE = {
    "kind": "metric_predicate",
    "entity": "entity.jaffle_customer",
    "input": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
    "op": ">",
    "value": 0,
}
# The same shape, but its measure sits on the customer's first-order clock, not the query's.
CROSS_CLOCK_PREDICATE = {
    **CUSTOMER_PREDICATE,
    "input": {"kind": "aggregate", "measure": "measure.jaffle.lifetime_spend_usd"},
}


def _filtered_revenue(predicate: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "aggregate",
        "measure": "measure.jaffle.revenue_usd",
        "aggregation": "sum",
        "filter": {"all": [{"expression": predicate}]},
    }


FILTERED_REVENUE = _filtered_revenue(CUSTOMER_PREDICATE)


def test_a_metric_predicate_in_an_aggregate_filter_is_not_collapsed(runtime: Runtime) -> None:
    """The predicate is tested per raw timestamp, so a total over the window would be wrong."""
    time = {"start": "2017-04-01", "end": "2017-04-04"}
    payload = _query([{"as": "revenue", "expression": FILTERED_REVENUE}], time)
    assert not plan_query(runtime._config, None, payload).time.get("window_total")

    # The same aggregate inside a metric recipe is found through the recipe.
    recipe = MetricConfig(
        id="metric.test.filtered_revenue",
        kind="derived",
        expression=parse_semantic_expression(FILTERED_REVENUE, context="query"),
    )
    config = replace(runtime._config, metric_recipes=[*runtime._config.metric_recipes, recipe])
    via_recipe = _query([{"as": "revenue", "expression": {"metric": recipe.id}}], time)
    assert not plan_query(config, None, via_recipe).time.get("window_total")

    response = runtime.query(payload)
    assert response["row_count"] > 1
    assert all(ORDER_TIME in row for row in response["rows"])
    assert "UNGRAINED_TIME_PROJECTION" in _codes(response)
    assert response["assumptions"] == []
    assert "time_shape" not in response


def test_a_cross_clock_predicate_in_an_aggregate_filter_is_refused(runtime: Runtime) -> None:
    """The window-total path must not turn a refused predicate into an answer."""
    time = {"start": "2017-04-01", "end": "2017-04-04"}
    select = [{"as": "revenue", "expression": _filtered_revenue(CROSS_CLOCK_PREDICATE)}]
    payload = _query(select, time)
    assert not plan_query(runtime._config, None, payload).time.get("window_total")
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(payload)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"


# Each case is a query over 2017-04-01..2017-07-01 (unless it names its own role and window)
# and raw SQL for the same answer. The gold columns are named like the response keys.
ORDER_WINDOW = "o.ordered_at >= TIMESTAMP '2017-04-01' AND o.ordered_at < TIMESTAMP '2017-07-01'"
STORE_JOIN = "JOIN jaffle_store s ON o.store_id = s.store_id"
INVENTORY_TIME = "temporal_role.jaffle_inventory_day"
INVENTORY = {"expression": {"metric": "metric.sales.inventory_on_hand_eop"}, "as": "inventory"}
INVENTORY_SPAN = {"temporal_role": INVENTORY_TIME, "start": "2016-09-01", "end": "2017-04-02"}
INVENTORY_WINDOW = "i.date_day >= DATE '2016-09-01' AND i.date_day < DATE '2017-04-02'"
LAST_SNAPSHOT = (
    "SELECT store_id, inventory_on_hand FROM (SELECT i.store_id, i.inventory_on_hand, "
    "ROW_NUMBER() OVER (PARTITION BY i.store_id ORDER BY i.date_day DESC) AS n "
    f"FROM jaffle_store_inventory_snapshot i WHERE {INVENTORY_WINDOW}) WHERE n = 1"
)


def _measure(measure: str, alias: str, aggregation: str = "") -> dict[str, Any]:
    expression = {"measure": f"measure.jaffle.{measure}"}
    if aggregation:
        expression["aggregation"] = aggregation
    return {"as": alias, "expression": expression}


BIG_STORE_FILTER = {
    "expression": {
        "kind": "comparison",
        "op": ">",
        "left": {"measure": "measure.jaffle.revenue_usd"},
        "right": {"kind": "literal", "value": 130000},
    },
    "op": "=",
    "value": True,
}

# id -> (query, group_by dimension ids, output aliases, gold SQL)
GOLD_CASES: dict[str, tuple[dict[str, Any], list[str], list[str], str]] = {
    "count_distinct direct": (
        _query(
            [_measure("order_count", "orders", "count_distinct")],
            {"start": "2017-04-01", "end": "2017-07-01"},
            group_by=[STORE],
        ),
        [STORE],
        ["orders"],
        f'SELECT s.store_name AS "{STORE}", COUNT(DISTINCT o.order_id) AS orders '
        f"FROM jaffle_order o {STORE_JOIN} WHERE {ORDER_WINDOW} GROUP BY 1",
    ),
    "count_distinct through the entity-in-terms-of rewrite": (
        _query(
            [_measure("order_count", "orders", "count_distinct")],
            {"start": "2017-04-01", "end": "2017-07-01"},
            group_by=["dimension.jaffle_product_type"],
        ),
        ["dimension.jaffle_product_type"],
        ["orders"],
        'SELECT p.product_type AS "dimension.jaffle_product_type", '
        "COUNT(DISTINCT o.order_id) AS orders FROM jaffle_item i "
        "JOIN jaffle_order o ON i.order_id = o.order_id JOIN jaffle_product p ON i.sku = p.sku "
        f"WHERE {ORDER_WINDOW} GROUP BY 1",
    ),
    "avg": (
        _query(
            [_measure("revenue_usd", "average_order", "avg")],
            {"start": "2017-04-01", "end": "2017-07-01"},
        ),
        [],
        ["average_order"],
        f"SELECT AVG(o.order_total_cents) / 100.0 AS average_order FROM jaffle_order o "
        f"WHERE {ORDER_WINDOW}",
    ),
    "median": (
        _query(
            [_measure("revenue_usd", "median_order", "median")],
            {"start": "2017-04-01", "end": "2017-07-01"},
        ),
        [],
        ["median_order"],
        f"SELECT MEDIAN(o.order_total_cents) / 100.0 AS median_order FROM jaffle_order o "
        f"WHERE {ORDER_WINDOW}",
    ),
    "ratio recipe": (
        _query(
            [{"as": "aov", "expression": {"metric": "metric.sales.aov_usd"}}],
            {"start": "2017-04-01", "end": "2017-07-01"},
        ),
        [],
        ["aov"],
        "SELECT SUM(o.order_total_cents) / 100.0 / COUNT(*) AS aov FROM jaffle_order o "
        f"WHERE {ORDER_WINDOW}",
    ),
    "metric_filters threshold on the window total": (
        _query(
            [REVENUE],
            {"start": "2017-04-01", "end": "2017-07-01"},
            group_by=[STORE],
            metric_filters=[BIG_STORE_FILTER],
        ),
        [STORE],
        ["revenue"],
        f'SELECT s.store_name AS "{STORE}", SUM(o.order_total_cents) / 100.0 AS revenue '
        f"FROM jaffle_order o {STORE_JOIN} WHERE {ORDER_WINDOW} GROUP BY 1 "
        "HAVING SUM(o.order_total_cents) / 100.0 > 130000",
    ),
    "semi-additive balance, all series": (
        _query([INVENTORY], INVENTORY_SPAN),
        [],
        ["inventory"],
        f"SELECT SUM(inventory_on_hand) AS inventory FROM ({LAST_SNAPSHOT})",
    ),
    "semi-additive balance per store": (
        _query(
            [INVENTORY],
            INVENTORY_SPAN,
            group_by=[STORE],
        ),
        [STORE],
        ["inventory"],
        f'SELECT s.store_name AS "{STORE}", l.inventory_on_hand AS inventory '
        f"FROM ({LAST_SNAPSHOT}) l JOIN jaffle_store s ON l.store_id = s.store_id",
    ),
    "two facts joined on the time key, per store": (
        _query(
            [REVENUE, _measure("item_revenue_usd", "item_revenue")],
            {"start": "2017-04-01", "end": "2017-07-01"},
            group_by=[STORE],
        ),
        [STORE],
        ["revenue", "item_revenue"],
        f'SELECT s.store_name AS "{STORE}", '
        "SUM(o.order_total_cents) / 100.0 AS revenue, "
        "SUM(i.item_revenue_cents) / 100.0 AS item_revenue "
        f"FROM (SELECT o.store_id, o.order_id, o.order_total_cents FROM jaffle_order o "
        f"WHERE {ORDER_WINDOW}) o "
        "JOIN (SELECT order_id, SUM(item_revenue_cents) AS item_revenue_cents "
        "FROM jaffle_item GROUP BY 1) i ON i.order_id = o.order_id "
        "JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY 1",
    ),
}


@pytest.mark.parametrize("case", list(GOLD_CASES))
def test_every_kind_of_measure_returns_the_window_total(runtime: Runtime, case: str) -> None:
    query, groups, values, sql = GOLD_CASES[case]
    response = runtime.query(query)
    gold = _gold(runtime, sql)

    assert gold, "the gold query must return rows"
    assert all(set(row) == {*groups, *values} for row in response["rows"])  # no time column
    got = {
        tuple(row[key] for key in groups): [row[key] for key in values] for row in response["rows"]
    }
    want = {tuple(row[key] for key in groups): [row[key] for key in values] for row in gold}
    assert got.keys() == want.keys()
    for key, row_values in want.items():
        assert got[key] == pytest.approx(row_values), key
    assert response["row_count"] == len(gold)  # one row per group, never one per timestamp
    assert "one total" in response["assumptions"][0]
    # An ungrouped average or median of order rows also says which rows it runs over.
    assert len(response["assumptions"]) == 1 + (case in {"avg", "median"})
    assert not {"UNGRAINED_TIME_PROJECTION", "UNGRAINED_GROUPED_TIME_PROJECTION"} & set(
        _codes(response)
    )


def test_a_balance_is_the_last_snapshot_in_the_window_not_a_sum(runtime: Runtime) -> None:
    balance = runtime.query(_query([INVENTORY], INVENTORY_SPAN))["rows"][0]["inventory"]
    summed = _gold(
        runtime,
        "SELECT SUM(inventory_on_hand) AS inventory FROM jaffle_store_inventory_snapshot i "
        f"WHERE {INVENTORY_WINDOW}",
    )[0]["inventory"]
    assert balance == 1085 + 860  # each store's last snapshot in the window
    assert balance != summed


@pytest.mark.parametrize("case", ["count_distinct through the entity-in-terms-of rewrite", "avg"])
def test_a_leaf_that_still_groups_by_the_raw_time_is_refused(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A leaf that misses the constant time key must not be passed off as one total."""
    monkeypatch.setattr(sql_lowering, "_time_bucket_expr", lambda time, raw_expr, config: raw_expr)
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(GOLD_CASES[case][0])
    assert raised.value.code == "WINDOW_TOTAL_UNSUPPORTED"
    assert raised.value.details["time_keys"] == [ORDER_TIME]


def test_the_default_cap_is_32k_characters() -> None:
    assert MCP_DEFAULT_MAX_RESULT_CHARS == 32_000


@pytest.fixture()
def mcp(runtime: Runtime) -> SemanticLayerMCPAdapter:
    return SemanticLayerMCPAdapter(runtime)


def test_execute_reports_the_total_and_its_assumption(mcp: SemanticLayerMCPAdapter) -> None:
    query = _query([REVENUE], {"start": "2017-04-01", "end": "2017-07-01"}, group_by=[STORE])
    response = mcp.call_tool("execute", {"query": query})
    assert response["ok"], response["errors"]
    assert response["row_count"] == 2 and response["truncated"] is False
    # The default response is minimal, and an assumption changes what the numbers mean.
    assert "one total" in response["assumptions"][0]
    assert response["time_shape"] == "window_total"
    assert "UNGRAINED_GROUPED_TIME_PROJECTION" not in _codes(response)
    yearly = mcp.call_tool(
        "execute", {"query": {**query, "time": {"temporal_role": ORDER_TIME, "grain": "year"}}}
    )
    assert "assumptions" not in yearly and "time_shape" not in yearly


def test_a_grant_scoped_execute_reports_the_total_and_its_flag(
    mcp: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = RequestContext(
        actor="subject",
        roles=("analyst",),
        audience="finance",
        metric_allowlist=("metric.sales.aov_usd",),
        dimension_allowlist=(STORE, ORDER_TIME),
    )
    aov = {"as": "aov", "expression": {"metric": "metric.sales.aov_usd"}}
    query = _query([aov], {"start": "2017-04-01", "end": "2017-07-01"}, group_by=[STORE])
    query["policy_context"] = grant.to_policy_context()
    response = mcp.call_tool("execute", {"query": query})
    assert response["ok"], response["errors"]
    assert response["row_count"] == 2
    assert all(set(row) == {STORE, "aov"} for row in response["rows"])  # no time column
    assert [column["semantic_id"] for column in response["output_columns"]] == [
        STORE,
        "metric.sales.aov_usd",
    ]
    assert "one total" in response["assumptions"][0]
    assert response["time_shape"] == "window_total"
    assert "UNGRAINED_GROUPED_TIME_PROJECTION" not in _codes(response)
    # The advice for a result that is too big must not blame the raw timestamp either.
    monkeypatch.setenv(LIMIT_ENV, "50")
    refused = mcp.call_tool("execute", {"query": query})
    assert refused["errors"][0]["code"] == "RESULT_TOO_LARGE"
    assert "raw timestamp" not in refused["errors"][0]["message"]


def test_an_oversized_result_is_refused_with_its_row_count(
    mcp: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    daily = _query([REVENUE], {"grain": "day"})
    fits = mcp.call_tool("execute", {"query": daily, "max_rows": 5})
    assert fits["ok"] and fits["row_count"] == 5

    monkeypatch.setenv(LIMIT_ENV, "2000")
    response = mcp.call_tool("execute", {"query": daily, "max_rows": 400})
    assert response["ok"] is False
    assert "rows" not in response
    error = response["errors"][0]
    assert error["code"] == "RESULT_TOO_LARGE"
    assert "365 rows" in error["message"]
    assert "coarser time.grain" in error["message"]
    details = error["details"]
    assert details["total_row_count"] == 365 and details["row_count"] == 365
    assert details["max_result_chars"] == 2000 and details["result_chars"] > 2000


def test_a_capped_result_that_is_still_too_large_names_the_full_count(
    mcp: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LIMIT_ENV, "2000")
    role_only = _query([REVENUE], {}, group_by=[STORE])
    response = mcp.call_tool("execute", {"query": role_only})
    error = response["errors"][0]
    assert error["code"] == "RESULT_TOO_LARGE"
    assert "more than 10,000 rows" in error["message"]
    assert "time.grain" in error["message"]


@pytest.mark.parametrize("bad", ["", "0", "-5", "many"])
def test_a_bad_limit_falls_back_to_the_default(
    mcp: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv(LIMIT_ENV, bad)
    query = _query([REVENUE], {"grain": "month"})
    assert mcp.call_tool("execute", {"query": query})["row_count"] == 12
