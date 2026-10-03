"""Empty groups: NULL when there is no data, 0 when there is data of nothing.

A group whose rows exist but whose values are all NULL has no data for that sum: NULL.

Gold values come from raw SQL on the seeded tables. The differential corpus in
``tests/integration/correctness`` holds the same rule to independent SQL on DuckDB and Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails import compiler
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.compiler_parts.empty_groups import resolves_to_zero, sql_nodes
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_config_expression
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime, _no_data_in_scope_warnings
from semantic_rails.schema import MetricConfig
from semantic_rails.sql_ast import SqlCte
from tests.integration.correctness.conftest import _write_variant
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.empty_groups_invariant import assert_settled_in_one_place
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_rendered_sql_snapshots import SNAPSHOT_CASES

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
ORDER_ID = "dimension.jaffle_order_id"
REVENUE = {"measure": "measure.jaffle.revenue_usd"}
ORDERS = {"measure": "measure.jaffle.order_count"}
ITEMS = {"measure": "measure.jaffle.item_count"}
NO_SUCH_STORE = [{"field": STORE, "op": "=", "value": "No such store"}]


def _select(**expressions: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"expression": expression, "as": alias} for alias, expression in expressions.items()]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """One runtime for the module (every query here only reads), so the data is seeded once."""
    package_dir = copy_package_config(tmp_path_factory.mktemp("empty_groups"), "jaffle_shop")
    package = load_package_config(str(package_dir))
    rt = Runtime.from_config(package, source_path=str(package_dir), package_id="jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


@pytest.fixture(scope="module")
def config() -> Any:
    return load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))


def _gold(runtime: Runtime, sql: str) -> list[dict[str, Any]]:
    """Run raw SQL on the runtime's own database, bypassing the compiler."""
    return list(runtime._get_adapter().query(sql))


def _warnings(response: dict[str, Any], code: str = "NO_DATA_IN_SCOPE") -> list[dict[str, Any]]:
    return [item for item in response["warnings"] if item.get("code") == code]


# -- the predicate -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("aggregation", "measure_class", "additive", "expected"),
    [
        ("sum", "additive", True, True),
        ("count", "event_count", True, True),
        ("count_distinct", "entity_count", True, True),
        ("", "additive", True, True),  # the measure's own default aggregation is a sum
        ("avg", "additive", True, False),
        ("min", "additive", True, False),
        ("max", "additive", True, False),
        ("median", "additive", True, False),
        ("sum", "semi_additive", True, False),  # a stock has no value for nothing
        ("count_distinct", "distinct_population", True, False),
        ("sum", "additive", False, False),  # already-aggregated values are never made up
    ],
)
def test_only_sums_and_counts_of_additive_measures_resolve_to_zero(
    config: Any, aggregation: str, measure_class: str, additive: bool, expected: bool
) -> None:
    measure = replace(
        next(row for row in config.measures if row.id == "measure.jaffle.revenue_usd"),
        measure_class=measure_class,
        additive=additive,
        default_aggregation="sum",
    )
    assert resolves_to_zero(aggregation, measure) is expected
    assert resolves_to_zero(aggregation, None) is False


# -- 0 where the measure has data elsewhere, NULL where it has none -----------------------


def test_a_count_beside_a_second_fact_reads_zero_in_its_empty_group(runtime: Runtime) -> None:
    window = {"start": "2017-04-01", "end": "2017-04-08"}
    response = runtime.query(
        {
            "version": 2,
            "select": _select(revenue=REVENUE, items=ITEMS),
            "group_by": [ORDER_ID],
            "time": {"temporal_role": ORDER_TIME, **window},
        }
    )
    gold = _gold(
        runtime,
        "SELECT o.order_id AS id, o.order_total_cents / 100.0 AS revenue, "
        "COUNT(DISTINCT i.item_id) AS items "
        "FROM jaffle_order o LEFT JOIN jaffle_item i ON i.order_id = o.order_id "
        "WHERE o.ordered_at >= TIMESTAMP '2017-04-01' AND o.ordered_at < TIMESTAMP '2017-04-08' "
        "GROUP BY 1, 2",
    )
    assert any(row["items"] == 0 for row in gold)  # the orders with no items this checks
    got = {row[ORDER_ID]: (row["revenue"], row["items"]) for row in response["rows"]}
    assert got == {row["id"]: (pytest.approx(row["revenue"]), row["items"]) for row in gold}
    assert not _warnings(response)


def test_a_limit_and_a_metric_filter_cannot_change_what_the_guard_sees(runtime: Runtime) -> None:
    """The orders with no items are all-zero once filtered, but the measure has items elsewhere."""
    query = {
        "version": 2,
        "select": _select(orders=ORDERS),
        "group_by": [ORDER_ID],
        "metric_filters": [{"expression": ITEMS, "op": "=", "value": 0}],
        "limit": 5,
    }
    response = runtime.query(query)
    assert response["row_count"] == 5
    assert not _warnings(response)
    # The settled value, not a NULL turned into 0 after the filter: the same orders with no
    # limit are the gold orders that have no items, each with an order count of 1.
    unlimited = runtime.query({**query, "limit": None})
    gold = _gold(
        runtime,
        "SELECT o.order_id AS id FROM jaffle_order o "
        "WHERE NOT EXISTS (SELECT 1 FROM jaffle_item i WHERE i.order_id = o.order_id)",
    )
    assert {row[ORDER_ID] for row in unlimited["rows"]} == {row["id"] for row in gold}
    assert all(row["orders"] == 1 for row in unlimited["rows"] + response["rows"])
    assert {row[ORDER_ID] for row in response["rows"]} <= {row["id"] for row in gold}


def test_a_filter_that_matches_nothing_reads_null_and_says_so(runtime: Runtime) -> None:
    query = {
        "version": 2,
        "select": _select(revenue=REVENUE, orders=ORDERS),
        "where": NO_SUCH_STORE,
    }
    response = runtime.query(query)
    raw = _gold(
        runtime,
        "SELECT COUNT(*) AS orders FROM jaffle_order o JOIN jaffle_store s "
        "ON s.store_id = o.store_id WHERE s.store_name = 'No such store'",
    )
    assert raw[0]["orders"] == 0  # the raw count of nothing is 0, and this answer is not
    assert response["rows"] == [{"revenue": None, "orders": None}]
    (warning,) = _warnings(response)
    assert warning["details"]["outputs"] == ["revenue", "orders"]
    assert warning["severity"] == "warning"


def test_only_the_input_with_no_data_reads_null_beside_one_that_has_data(runtime: Runtime) -> None:
    none = {**REVENUE, "kind": "aggregate", "filter": {"all": NO_SUCH_STORE}}
    response = runtime.query(
        {
            "version": 2,
            "select": _select(revenue=REVENUE, none=none),
            "time": {"temporal_role": ORDER_TIME, "grain": "quarter"},
        }
    )
    assert response["row_count"] > 1
    assert all(row["revenue"] is not None and row["none"] is None for row in response["rows"])
    (warning,) = _warnings(response)
    assert warning["details"]["outputs"] == ["none"]


def test_an_average_of_nothing_is_undefined_not_missing_data(runtime: Runtime) -> None:
    average = {
        **REVENUE,
        "kind": "aggregate",
        "aggregation": "avg",
        "filter": {"all": NO_SUCH_STORE},
    }
    response = runtime.query({"version": 2, "select": _select(average=average)})
    assert response["rows"] == [{"average": None}]
    assert not _warnings(response)


def test_no_rows_and_no_time_window_says_nothing_matched(runtime: Runtime) -> None:
    query = {
        "version": 2,
        "select": _select(revenue=REVENUE),
        "group_by": [STORE],
        "where": NO_SUCH_STORE,
    }
    response = runtime.query(query)
    assert response["rows"] == []
    assert [item["details"]["outputs"] for item in _warnings(response)] == [["revenue"]]
    windowed = runtime.query(
        {**query, "time": {"temporal_role": ORDER_TIME, "start": "2017-04-01", "end": "2017-05-01"}}
    )
    # A window with no rows is the window warning's to explain.
    assert not _warnings(windowed)
    assert _warnings(windowed, "EMPTY_RESULT_WINDOW")


def test_an_output_with_a_reason_of_its_own_to_be_null_never_gets_the_warning(
    runtime: Runtime,
) -> None:
    """A prior-period output is NULL on every row of a short series, however much data there is."""
    prior = {"kind": "prior_period", "input": ORDERS, "offset": {"unit": "year", "value": 1}}
    response = runtime.query(
        {
            "version": 2,
            "select": _select(orders=ORDERS, prior_year=prior),
            "time": {"temporal_role": ORDER_TIME, "grain": "month", "end": "2017-06-01"},
        }
    )
    assert 1 < response["row_count"] <= 12
    assert all(row["orders"] > 0 and row["prior_year"] is None for row in response["rows"])
    assert not _warnings(response)


def test_a_metric_filter_that_removes_every_group_is_not_missing_data(runtime: Runtime) -> None:
    response = runtime.query(
        {
            "version": 2,
            "select": _select(revenue=REVENUE),
            "group_by": [STORE],
            "metric_filters": [{"expression": ORDERS, "op": ">", "value": 100000}],
        }
    )
    assert response["rows"] == []
    assert not _warnings(response)


def test_the_query_mcp_carries_the_warning_at_its_default_verbosity(runtime: Runtime) -> None:
    query = {"version": 2, "select": _select(revenue=REVENUE), "where": NO_SUCH_STORE}
    response = SemanticLayerMCPAdapter(runtime).call_tool("execute", {"query": query})
    assert response["ok"], response["errors"]
    assert [item["code"] for item in _warnings(response)] == ["NO_DATA_IN_SCOPE"]


@pytest.mark.parametrize("truncated", [False, True])
def test_a_clipped_result_is_never_called_empty(truncated: bool) -> None:
    class Rows(list):
        pass

    rows = Rows([{"revenue": None}])
    rows.truncated = truncated  # type: ignore[attr-defined]
    compiled = {
        "zero_outputs": [{"output": "revenue", "measures": ["measure.jaffle.revenue_usd"]}],
        "logical_plan": SimpleNamespace(time={}),
    }
    assert bool(_no_data_in_scope_warnings(compiled, rows)) is not truncated


# -- one place settles them, and a path that skips it is refused ---------------------------

DISTRIBUTION = {
    "kind": "distribution",
    "function": "median",
    "over": {"kind": "entity_value", "entity": "entity.jaffle_order", "input": REVENUE},
}
SHAPES = {
    **{name: query for name, (query, _sql) in SNAPSHOT_CASES.items()},
    "metric_filter": {
        "select": _select(orders=ORDERS),
        "group_by": [ORDER_ID],
        "metric_filters": [{"expression": ITEMS, "op": "=", "value": 0}],
    },
    "beside_a_distribution": {
        "select": _select(revenue=REVENUE, median=DISTRIBUTION),
        "time": {"temporal_role": ORDER_TIME, "grain": "month"},
    },
    "distribution_alone": {"select": _select(median=DISTRIBUTION)},
    "window_total": {
        "select": _select(revenue=REVENUE, orders=ORDERS),
        "time": {"temporal_role": ORDER_TIME, "start": "2017-04-01", "end": "2017-05-01"},
    },
    "sum_of_two_measures": {
        "select": _select(
            both={"kind": "arithmetic", "op": "add", "left": REVENUE, "right": ITEMS}
        ),
        "group_by": [STORE],
    },
    "average_only": {
        "select": _select(average={**REVENUE, "kind": "aggregate", "aggregation": "avg"}),
        "group_by": [STORE],
    },
    "filled_series": {
        "select": _select(revenue=REVENUE),
        "time": {"temporal_role": ORDER_TIME, "grain": "month", "fill": True},
    },
    # A threshold that 0 passes takes the anti-join, which must not coalesce the value.
    "predicate_case_count": {
        "select": _select(orders=ORDERS),
        "group_by": [STORE],
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer",
                    "scope_mode": "entity_only",
                    "input": {"measure": "measure.jaffle.large_order_count"},
                    "op": "=",
                    "value": 0,
                },
                "op": "=",
                "value": True,
            }
        ],
    },
    "predicate_zero_passes": {
        "select": _select(revenue=REVENUE),
        "group_by": [STORE],
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer",
                    "scope_mode": "entity_only",
                    "input": {
                        "kind": "arithmetic",
                        "op": "subtract",
                        "left": ORDERS,
                        "right": {**ORDERS, "kind": "aggregate", "filter": {"all": NO_SUCH_STORE}},
                    },
                    "op": "<",
                    "value": 1,
                },
                "op": "=",
                "value": True,
            }
        ],
    },
}


@pytest.mark.parametrize("shape", SHAPES)
def test_every_sum_and_count_a_projection_reads_comes_from_the_guard(
    config: Any, shape: str
) -> None:
    compiled = compile_query(config, Registry(config), {"version": 2, **SHAPES[shape]})
    assert_settled_in_one_place(compiled, config)


@pytest.mark.parametrize(
    ("shape", "patched"),
    [("single_measure", "zero_aliases"), ("beside_a_distribution", "zero_outputs")],
)
def test_a_lowering_path_that_skips_the_guard_is_refused(
    config: Any, monkeypatch: pytest.MonkeyPatch, shape: str, patched: str
) -> None:
    """Force the bypass: lowering builds no guard, and the check that works it out again refuses."""
    monkeypatch.setattr(sql_lowering, patched, lambda *args: {})
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), {"version": 2, **SHAPES[shape]})
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"


@pytest.mark.parametrize(
    "shape",
    [
        "single_measure",
        "same_source_multi_measure",
        "filled_series",
        "window_total",
        "sum_of_two_measures",
    ],
)
def test_a_leaf_that_skips_the_row_count_is_refused(
    config: Any, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Force the bypass: no leaf counts its rows, so the guard can't tell a group with no rows
    from one whose values are all unknown, and refuses rather than read 0."""
    monkeypatch.setattr(sql_lowering, "_row_markers", lambda *args: [])
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), {"version": 2, **SHAPES[shape]})
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert raised.value.details["missing"] == "row_count"


@pytest.mark.parametrize("shape", ["predicate_case_count", "predicate_zero_passes"])
def test_a_predicate_source_that_skips_the_guard_is_refused(
    config: Any, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """An entity the source doesn't list reads like the ones it does only while the source is
    settled as a whole, so a source compiled without the guard is refused, not gated."""

    def unguarded(config: Any, payload: dict[str, Any]) -> Any:
        return compiler._compile_query_sql_ast(config, payload, project_cut=True, guard_empty=False)

    monkeypatch.setattr(compiler, "_compile_predicate_source_ast", unguarded)
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), {"version": 2, **SHAPES[shape]})
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"


# -- rows whose values are all unknown are not a group with no rows -------------------------
# The differential corpus's shop: order 7 is store a's only order in May 2024 and has no
# amount; order 8 (May) has no store; store b's only order of May and June is order 9 (June).

SHOP_ORDER = "dimension.shop_order_id"
SHOP_STORE = "dimension.shop_order_store_id"
SHOP_MONTH = {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "month"}
SHOP_REVENUE = {"measure": "measure.shop.revenue"}
SHOP_ORDERS = {"measure": "measure.shop.order_count"}
SHOP_GOODS = {"measure": "measure.shop.goods_refunded"}
SHOP_SHIPPING = {"measure": "measure.shop.shipping_refunded"}
IN_STORE_A = {
    "kind": "comparison",
    "op": "=",
    "left": {"kind": "column", "column": "store_id", "entity": "entity.shop_order"},
    "right": {"kind": "literal", "value": "a"},
}
IN_STORE_B = {**IN_STORE_A, "right": {"kind": "literal", "value": "b"}}


@pytest.fixture(scope="module")
def shop_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_variant(tmp_path_factory.mktemp("shop"), "utc_authored")


@pytest.fixture(scope="module")
def shop(shop_package: Path) -> Iterator[Runtime]:
    rt = Runtime.from_path(str(shop_package))
    try:
        yield rt
    finally:
        rt.close()


@pytest.mark.parametrize(
    ("revenue_alias", "median_alias"),
    [
        ("agent_branch_1__rows", "median"),
        ("revenue", "agent_branch_1__rows"),
        ("revenue", "AGENT_BRANCH_1__ROWS"),
        ("agent_branch_1__rows", "agent_branch_1__rows_2"),
    ],
)
@pytest.mark.parametrize("by_store", [False, True])
def test_branch_row_markers_never_shadow_projected_values(
    shop: Runtime, revenue_alias: str, median_alias: str, by_store: bool
) -> None:
    """Outputs named like a branch's former hidden markers keep their values. Beside a
    distribution the earlier settlement applies: store a's May, whose only amount is unknown,
    reads 0 where revenue has data in scope."""
    distribution = {
        "kind": "distribution",
        "function": "median",
        "over": {"kind": "entity_value", "entity": "entity.shop_order", "input": SHOP_REVENUE},
    }
    response = shop.query(
        {
            "version": 1,
            "select": _select(**{revenue_alias: SHOP_REVENUE, median_alias: distribution}),
            "group_by": [SHOP_STORE] if by_store else [],
            "time": SHOP_MONTH,
        }
    )
    month = f"{SHOP_MONTH['temporal_role']}__month"
    got = {
        (row.get(SHOP_STORE), str(row[month])[:7]): (row[revenue_alias], row[median_alias])
        for row in typed_rows(response)
    }
    gold = _gold(
        shop,
        f"SELECT {'store_id' if by_store else 'NULL'} AS s, "
        "date_trunc('month', ordered_at) AS month, COALESCE(SUM(amount), 0) AS revenue, "
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) AS median "
        f"FROM orders GROUP BY {'1, 2' if by_store else '2'}",
    )
    assert got == {
        (row["s"], str(row["month"])[:7]): (row["revenue"], row["median"]) for row in gold
    }
    if by_store:
        assert got[("a", "2024-05")] == (0, None)
    else:
        assert got[(None, "2023-11")] == (15, 7.5)


# Goods plus shipping refunds beside the median order, without order 7: each refunded order
# has goods or shipping, never both, so every refund group reads one unknown operand.
REFUNDS_BESIDE_A_MEDIAN = {
    "version": 1,
    "select": _select(
        total={"kind": "arithmetic", "op": "add", "left": SHOP_GOODS, "right": SHOP_SHIPPING},
        median={
            "kind": "distribution",
            "function": "median",
            "over": {"kind": "entity_value", "entity": "entity.shop_order", "input": SHOP_REVENUE},
        },
    ),
    "group_by": [SHOP_STORE],
    "time": SHOP_MONTH,
    "where": [{"field": SHOP_ORDER, "op": "!=", "value": 7}],
}


def test_arithmetic_beside_a_distribution_keeps_its_empty_groups_zero(shop: Runtime) -> None:
    """Beside a distribution, every branch keeps the earlier settlement: an operand's unknown
    amounts read 0 where its measure has data in scope, so a group with no refunds at all
    (store a's November) is 0, not NULL."""
    response = shop.query(REFUNDS_BESIDE_A_MEDIAN)
    month = f"{SHOP_MONTH['temporal_role']}__month"
    got = {(row[SHOP_STORE], str(row[month])[:7]): row["total"] for row in typed_rows(response)}
    assert got[("a", "2023-11")] == 0
    # Orders 2 (goods 5 + 1), 4 (shipping 3) and 6 (goods 4); every other group has none.
    refunded = {("b", "2023-11"): 6, ("a", "2024-01"): 3, ("a", "2024-03"): 4}
    assert got == {key: refunded.get(key, 0) for key in got}
    assert len(got) == 10


# The same query's SQL at the commit before unknown amounts stayed NULL, unchanged.
REFUNDS_BESIDE_A_MEDIAN_SQL = """WITH agent_branch_1__leaf_1 AS (
SELECT
  orders.store_id AS g1,
  DATE_TRUNC('month', CAST(orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(refunds.goods_amount) AS m1,
  SUM(refunds.shipping_amount) AS m2
FROM refunds
INNER JOIN orders ON refunds.order_id = orders.order_id
WHERE
  refunds.order_id != 7
GROUP BY
  orders.store_id,
  DATE_TRUNC('month', CAST(orders.ordered_at AS TIMESTAMP))
),
agent_branch_1__guarded_base AS (
SELECT
  base.g1 AS g1,
  base.t AS t,
  CASE WHEN COUNT(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1,
  CASE WHEN COUNT(base.m2) OVER () > 0 THEN COALESCE(base.m2, 0) END AS m2
FROM agent_branch_1__leaf_1 AS base
),
agent_branch_1 AS (
SELECT
  base.g1 AS "dimension.shop_order_store_id",
  base.t AS "temporal_role.shop_order_ordered_at__month",
  base.m1 + base.m2 AS total
FROM agent_branch_1__guarded_base AS base
),
agent_branch_2__median__entity_values__leaf_1 AS (
SELECT
  orders.store_id AS g1,
  orders.order_id AS g2,
  DATE_TRUNC('month', CAST(orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(orders.amount) AS m1
FROM orders
WHERE
  orders.order_id != 7
GROUP BY
  orders.store_id,
  orders.order_id,
  DATE_TRUNC('month', CAST(orders.ordered_at AS TIMESTAMP))
),
agent_branch_2__median__entity_values AS (
SELECT
  base.g1 AS "dimension.shop_order_store_id",
  base.g2 AS "dimension.shop_order_id",
  base.t AS "temporal_role.shop_order_ordered_at__month",
  base.m1 AS __entity_value
FROM agent_branch_2__median__entity_values__leaf_1 AS base
),
agent_branch_2 AS (
SELECT
  agent_branch_2__median__entity_values."dimension.shop_order_store_id" AS "dimension.shop_order_store_id",
  agent_branch_2__median__entity_values."temporal_role.shop_order_ordered_at__month" AS "temporal_role.shop_order_ordered_at__month",
  MEDIAN(agent_branch_2__median__entity_values.__entity_value) AS median
FROM agent_branch_2__median__entity_values
GROUP BY
  agent_branch_2__median__entity_values."dimension.shop_order_store_id",
  agent_branch_2__median__entity_values."temporal_role.shop_order_ordered_at__month"
),
agent_combined_2 AS (
SELECT
  COALESCE(left_side."dimension.shop_order_store_id", right_side."dimension.shop_order_store_id") AS "dimension.shop_order_store_id",
  COALESCE(CAST(left_side."temporal_role.shop_order_ordered_at__month" AS TIMESTAMP), CAST(right_side."temporal_role.shop_order_ordered_at__month" AS TIMESTAMP)) AS "temporal_role.shop_order_ordered_at__month",
  left_side.total AS total,
  right_side.median AS median
FROM agent_branch_1 AS left_side
FULL OUTER JOIN agent_branch_2 AS right_side ON left_side."dimension.shop_order_store_id" IS NOT DISTINCT FROM right_side."dimension.shop_order_store_id" AND CAST(left_side."temporal_role.shop_order_ordered_at__month" AS TIMESTAMP) IS NOT DISTINCT FROM CAST(right_side."temporal_role.shop_order_ordered_at__month" AS TIMESTAMP)
),
guarded_base AS (
SELECT
  base."dimension.shop_order_store_id" AS "dimension.shop_order_store_id",
  base."temporal_role.shop_order_ordered_at__month" AS "temporal_role.shop_order_ordered_at__month",
  CASE WHEN COUNT(base.total) OVER () > 0 THEN COALESCE(base.total, 0) END AS total,
  base.median AS median
FROM agent_combined_2 AS base
),
agent_projected AS (
SELECT
  base."dimension.shop_order_store_id" AS "dimension.shop_order_store_id",
  base."temporal_role.shop_order_ordered_at__month" AS "temporal_role.shop_order_ordered_at__month",
  base.total AS total,
  base.median AS median
FROM guarded_base AS base
)
SELECT
  agent_projected."dimension.shop_order_store_id" AS "dimension.shop_order_store_id",
  agent_projected."temporal_role.shop_order_ordered_at__month" AS "temporal_role.shop_order_ordered_at__month",
  agent_projected.total AS total,
  agent_projected.median AS median
FROM agent_projected
ORDER BY
  "temporal_role.shop_order_ordered_at__month" ASC,
  "dimension.shop_order_store_id" ASC"""


def test_a_query_beside_a_distribution_lowers_as_it_did_before(shop_package: Path) -> None:
    config = load_package_config(str(shop_package))
    compiled = compile_query(config, Registry(config), REFUNDS_BESIDE_A_MEDIAN)
    assert compiled["sql"] == REFUNDS_BESIDE_A_MEDIAN_SQL


def test_an_output_named_like_a_row_count_keeps_its_name_in_order_by(shop: Runtime) -> None:
    """The leaf's hidden row count has a name of its own; a caller's alias like the measure's
    leaf alias plus ``__rows`` is the caller's, so ordering by it orders by the output."""
    alias = "leaf__measure_shop_revenue__sum__rows"
    response = shop.query(
        {
            "version": 1,
            "select": _select(**{alias: SHOP_REVENUE}),
            "group_by": [SHOP_STORE],
            "order_by": [{"field": alias, "direction": "desc"}],
        }
    )
    got = [(row[SHOP_STORE], row[alias]) for row in typed_rows(response)]
    gold = _gold(
        shop, "SELECT store_id AS s, SUM(amount) AS revenue FROM orders GROUP BY 1 ORDER BY 2 DESC"
    )
    assert got == [(row["s"], row["revenue"]) for row in gold] == [("a", 43), ("b", 25), (None, 6)]


def _renamed_amount(root: Path, extra: str) -> Runtime:
    """The shop with its amount column named like revenue's leaf alias plus ``__rows`` (and,
    with ``extra``, a column ``m1_rows`` holding 100 on every order), read by revenue."""
    package = _write_variant(root, "utc_authored")
    seed = package / "data" / "seed.sql"
    statements = [f"ALTER TABLE orders RENAME COLUMN amount TO {RENAMED_AMOUNT}"]
    if extra:
        statements += [
            "ALTER TABLE orders ADD COLUMN m1_rows INTEGER",
            "UPDATE orders SET m1_rows = 100",
        ]
    seed.write_text(seed.read_text(encoding="utf-8") + "".join(f"\n{item};" for item in statements))
    config = load_package_config(str(package))
    column = parse_config_expression({"kind": "column", "column": RENAMED_AMOUNT})
    config = replace(
        config,
        aggregate_relations=[],
        measures=[
            replace(row, expr=column) if row.id == SHOP_REVENUE["measure"] else row
            for row in config.measures
        ],
    )
    return Runtime.from_config(config, source_path=str(package))


RENAMED_AMOUNT = "leaf__measure_shop_revenue__sum__rows"
EVERY_M1_ROWS = {
    "kind": "aggregate_if",
    "aggregation": "sum",
    "condition": {**IN_STORE_A, "op": "!="},
    "value": {"kind": "column", "column": "m1_rows", "entity": "entity.shop_order"},
}


@pytest.mark.parametrize("extra", ["", "unrelated", "read"])
def test_a_physical_column_named_like_a_row_count_is_read_as_itself(
    tmp_path: Path, extra: str
) -> None:
    """No rewrite reaches a physical column: revenue reads its own column, whatever it is
    named, and never ``m1_rows``. The hidden row count takes a name no column the query reads
    has, so beside a sum of ``m1_rows`` it is ``m1_rows_2``."""
    rt = _renamed_amount(tmp_path, extra)
    try:
        items = {"revenue": SHOP_REVENUE, **({"other": EVERY_M1_ROWS} if extra == "read" else {})}
        response = rt.query({"version": 1, "select": _select(**items)})
        (row,) = typed_rows(response)
        assert row["revenue"] == 74
        if extra == "read":  # store b's orders 2, 5, 9 and 10
            assert row["other"] == 400
        sql = response["rendered_sql"]
        assert f"orders.{RENAMED_AMOUNT}" in sql
        assert ("AS m1_rows_2" in sql) is (extra == "read")
    finally:
        rt.close()


def test_a_row_count_named_like_another_column_is_refused(
    config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the bypass: name the row count like the query's output, and the check refuses."""
    monkeypatch.setattr(
        sql_lowering,
        "_row_count_names",
        lambda plan, taken: {row.bound_measure.alias: "Revenue" for row in plan.measure_plans},
    )
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), {"version": 2, **SHAPES["single_measure"]})
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert raised.value.details["row_counts_named_like"] == ["revenue"]


@pytest.mark.parametrize("else_value", [None, 0, 2])
@pytest.mark.parametrize("by_store", [False, True])
def test_case_sum_preserves_explicit_else_contributions(
    shop_package: Path, else_value: int | None, by_store: bool
) -> None:
    config = load_package_config(str(shop_package))
    expression = parse_config_expression(
        {
            "kind": "case",
            "whens": [{"when": IN_STORE_A, "then": {"kind": "column", "column": "amount"}}],
            "else": {"kind": "literal", "value": else_value},
        }
    )
    config = replace(
        config,
        aggregate_relations=[],
        measures=[
            replace(row, expr=expression) if row.id == SHOP_REVENUE["measure"] else row
            for row in config.measures
        ],
    )
    rt = Runtime.from_config(config, source_path=str(shop_package))
    try:
        response = rt.query(
            {
                "version": 1,
                "select": _select(revenue=SHOP_REVENUE),
                "group_by": [SHOP_STORE] if by_store else [],
                "time": SHOP_MONTH,
            }
        )
        month = f"{SHOP_MONTH['temporal_role']}__month"
        got = {
            (row.get(SHOP_STORE), str(row[month])[:7]): row["revenue"]
            for row in typed_rows(response)
        }
        literal = "NULL" if else_value is None else str(else_value)
        # ELSE NULL has no contributing row where the condition fails, so its empty sum
        # settles to 0. Explicit non-NULL ELSE values remain in the ordinary SQL sum.
        value = f"SUM(CASE WHEN store_id = 'a' THEN amount ELSE {literal} END)"
        if else_value is None:
            value = (
                f"CASE WHEN COUNT(CASE WHEN store_id = 'a' THEN 1 END) = 0 THEN 0 ELSE {value} END"
            )
        gold = _gold(
            rt,
            f"SELECT {'store_id' if by_store else 'NULL'} AS s, "
            f"date_trunc('month', ordered_at) AS month, {value} AS revenue "
            f"FROM orders GROUP BY {'1, 2' if by_store else '2'}",
        )
        assert got == {(row["s"], str(row["month"])[:7]): row["revenue"] for row in gold}
        if by_store:
            assert got[("a", "2024-05")] is None
        else:
            assert got[(None, "2024-05")] == else_value
    finally:
        rt.close()


@pytest.mark.parametrize("else_null", [False, True])
def test_a_case_with_two_branches_is_zero_only_where_no_row_meets_either(
    shop_package: Path, else_null: bool
) -> None:
    """A second branch is still a condition: a group none of whose rows meets a branch reads 0,
    as with one branch, and a group whose matching rows have no amount stays unknown."""
    amount = {"kind": "column", "column": "amount"}
    payload: dict[str, Any] = {
        "kind": "case",
        "whens": [{"when": IN_STORE_A, "then": amount}, {"when": IN_STORE_B, "then": amount}],
    }
    if else_null:
        payload["else"] = {"kind": "literal", "value": None}
    config = load_package_config(str(shop_package))
    config = replace(
        config,
        aggregate_relations=[],
        measures=[
            replace(row, expr=parse_config_expression(payload))
            if row.id == SHOP_REVENUE["measure"]
            else row
            for row in config.measures
        ],
    )
    rt = Runtime.from_config(config, source_path=str(shop_package))
    try:
        response = rt.query(
            {
                "version": 1,
                "select": _select(revenue=SHOP_REVENUE),
                "group_by": [SHOP_STORE],
                "time": SHOP_MONTH,
            }
        )
        month = f"{SHOP_MONTH['temporal_role']}__month"
        got = {
            (row[SHOP_STORE], str(row[month])[:7]): row["revenue"] for row in typed_rows(response)
        }
        gold = _gold(
            rt,
            "SELECT store_id AS s, date_trunc('month', ordered_at) AS month, "
            "CASE WHEN COUNT(CASE WHEN store_id = 'a' THEN 1 WHEN store_id = 'b' THEN 1 END) = 0 "
            "THEN 0 ELSE SUM(CASE WHEN store_id = 'a' THEN amount "
            "WHEN store_id = 'b' THEN amount END) END AS revenue FROM orders GROUP BY 1, 2",
        )
        assert got == {(row["s"], str(row["month"])[:7]): row["revenue"] for row in gold}
        # Order 8 has no store, so it meets neither branch; order 7 meets one with no amount.
        assert got[(None, "2024-05")] == 0
        assert got[("a", "2024-05")] is None
    finally:
        rt.close()


def test_a_conditional_sum_is_zero_only_where_no_row_meets_its_condition(shop: Runtime) -> None:
    store_a = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": IN_STORE_A,
        "value": {"kind": "column", "column": "amount", "entity": "entity.shop_order"},
    }
    response = shop.query({"version": 1, "select": _select(a=store_a), "group_by": [SHOP_ORDER]})
    got = {row[SHOP_ORDER]: row["a"] for row in typed_rows(response)}
    gold = _gold(
        shop,
        "SELECT order_id AS id, CASE WHEN COUNT(CASE WHEN store_id = 'a' THEN 1 END) = 0 "
        "THEN 0 ELSE SUM(CASE WHEN store_id = 'a' THEN amount END) END AS a "
        "FROM orders GROUP BY 1",
    )
    assert got == {row["id"]: row["a"] for row in gold}
    # Order 7 meets the condition with no amount: unknown. Store b's and the storeless order
    # meet it with no row: 0, as the sum has amounts elsewhere.
    assert got[7] is None
    assert got[2] == got[8] == 0
    assert got[1] == 10


def _case_under_arithmetic_config(shop_package: Path) -> Any:
    config = load_package_config(str(shop_package))
    expr = parse_config_expression(
        {
            "kind": "arithmetic",
            "op": "divide",
            "left": {
                "kind": "case",
                "whens": [{"when": IN_STORE_A, "then": {"kind": "column", "column": "amount"}}],
            },
            "right": {"kind": "literal", "value": 100.0},
        }
    )
    return replace(
        config,
        measures=[
            replace(row, expr=expr) if row.id == SHOP_REVENUE["measure"] else row
            for row in config.measures
        ],
    )


@pytest.mark.parametrize("unknown_only", [False, True])
def test_a_case_under_arithmetic_keeps_the_base_settlement(
    shop_package: Path, unknown_only: bool
) -> None:
    """The fallback makes a no-match group zero when amounts are observed elsewhere; a scope
    containing only the matched unknown amount remains NULL, as on the base path."""
    config = replace(_case_under_arithmetic_config(shop_package), aggregate_relations=[])
    raw = next(
        row
        for row in load_package_config(str(shop_package)).measures
        if row.id == SHOP_REVENUE["measure"]
    )
    config = replace(
        config, measures=[*config.measures, replace(raw, id="measure.shop.raw_revenue")]
    )
    rt = Runtime.from_config(config, source_path=str(shop_package))
    try:
        query = {
            "version": 1,
            "select": _select(revenue=SHOP_REVENUE, raw={"measure": "measure.shop.raw_revenue"}),
            "group_by": [SHOP_STORE],
            "time": SHOP_MONTH,
            **({"where": [{"field": SHOP_ORDER, "op": "=", "value": 7}]} if unknown_only else {}),
        }
        response = rt.query(query)
        month = f"{SHOP_MONTH['temporal_role']}__month"
        got = {
            (row[SHOP_STORE], str(row[month])[:7]): row["revenue"] for row in typed_rows(response)
        }
        gold = _gold(
            rt,
            "WITH amounts AS (SELECT store_id AS s, date_trunc('month', ordered_at) AS month, "
            "SUM(CASE WHEN store_id = 'a' THEN amount END / 100.0) AS revenue FROM orders "
            + ("WHERE order_id = 7 " if unknown_only else "")
            + "GROUP BY 1, 2) SELECT s, month, CASE WHEN COUNT(revenue) OVER () > 0 "
            "THEN COALESCE(revenue, 0) END AS revenue FROM amounts",
        )
        assert got == {(row["s"], str(row["month"])[:7]): row["revenue"] for row in gold}
        if unknown_only:
            assert got == {("a", "2024-05"): None}
        else:
            assert got[("b", "2023-11")] == 0
        # The nested CASE fallback must not turn an ordinary sum's unknown amount into zero.
        assert (
            next(
                row
                for row in typed_rows(response)
                if row[SHOP_STORE] == "a" and str(row[month])[:7] == "2024-05"
            )["raw"]
            is None
        )
        compiled = compile_query(config, Registry(config), query)
        assert_settled_in_one_place(compiled, config)
    finally:
        rt.close()


@pytest.mark.parametrize("beside_a_distribution", [False, True])
def test_a_rollup_never_answers_a_case_under_arithmetic(
    shop_package: Path, beside_a_distribution: bool
) -> None:
    config = _case_under_arithmetic_config(shop_package)
    query = {"select": _select(revenue=SHOP_REVENUE), "group_by": [SHOP_STORE], "time": SHOP_MONTH}
    if beside_a_distribution:
        query["select"] += REFUNDS_BESIDE_A_MEDIAN["select"][1:]
    compiled = compile_query(config, Registry(config), {"version": 1, **query})
    (leaf,) = compiled["logical_plan"].measure_plans
    assert leaf.aggregate_relation_id == ""
    assert set(leaf.aggregate_relation_rejections.values()) == {"aggregation_not_reaggregable"}


def test_a_nested_case_forced_onto_a_rollup_is_refused(
    shop_package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _case_under_arithmetic_config(shop_package)
    monkeypatch.setattr(
        compiler,
        "_select_aggregate_relation",
        lambda *args, **kwargs: (config.aggregate_relations[0].id, {}),
    )
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(
            config,
            Registry(config),
            {
                "version": 1,
                "select": _select(revenue=SHOP_REVENUE),
                "group_by": [SHOP_STORE],
                "time": SHOP_MONTH,
            },
        )
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert raised.value.details["measures"] == [SHOP_REVENUE["measure"]]


@pytest.mark.parametrize("composite", [False, True])
def test_a_source_rollup_count_never_shadows_a_physical_join_column(
    tmp_path: Path, composite: bool
) -> None:
    """Refunds reach the customer channel through orders; the intermediate count must not
    read a physical join column as a count of matching refunds."""
    package = _write_variant(tmp_path, "utc_authored")
    seed = package / "data" / "seed.sql"
    extra = (
        "ALTER TABLE refunds ADD COLUMN m1_rows INTEGER;\n"
        "UPDATE refunds SET m1_rows = 1;\n"
        "ALTER TABLE orders ADD COLUMN pair_key INTEGER;\n"
        "UPDATE orders SET pair_key = 1;\n"
        if composite
        else "ALTER TABLE refunds RENAME COLUMN order_id TO __source_rows;\n"
    )
    seed.write_text(seed.read_text(encoding="utf-8") + "\n" + extra)
    config = load_package_config(str(package))
    expr = parse_config_expression(
        {
            "kind": "case",
            "whens": [
                {
                    "when": {
                        "kind": "comparison",
                        "op": "=",
                        "left": {"kind": "column", "column": "refund_type"},
                        "right": {"kind": "literal", "value": "goods"},
                    },
                    "then": {"kind": "column", "column": "goods_amount"},
                }
            ],
        }
    )
    config = replace(
        config,
        entities=[
            replace(
                row,
                foreign_keys={
                    target: [
                        "__source_rows" if column == "order_id" else column for column in columns
                    ]
                    for target, columns in row.foreign_keys.items()
                },
            )
            if row.table == "refunds" and not composite
            else row
            for row in config.entities
        ],
        relationships=[
            replace(
                row,
                source_column="" if composite else "__source_rows",
                target_column="" if composite else row.target_column,
                source_columns=["order_id", "m1_rows"] if composite else ["__source_rows"],
                target_columns=["order_id", "pair_key"] if composite else row.target_columns,
            )
            if row.source_entity == "entity.shop_refund" and row.source_column == "order_id"
            else row
            for row in config.relationships
        ],
        measures=[
            replace(row, expr=expr) if row.id == SHOP_GOODS["measure"] else row
            for row in config.measures
        ],
    )
    rt = Runtime.from_config(config, source_path=str(package))
    try:
        channel = "dimension.shop_customer_channel"
        query = {"version": 1, "select": _select(goods=SHOP_GOODS), "group_by": [channel]}
        response = rt.query(query)
        got = {(row[channel], row["goods"]) for row in typed_rows(response)}
        join = (
            "r.order_id = o.order_id AND r.m1_rows = o.pair_key"
            if composite
            else "r.__source_rows = o.order_id"
        )
        gold = _gold(
            rt,
            "SELECT s.channel, CASE WHEN COUNT(CASE WHEN r.refund_type = 'goods' THEN 1 END) = 0 "
            "THEN 0 ELSE SUM(CASE WHEN r.refund_type = 'goods' THEN r.goods_amount END) END AS goods "
            f"FROM refunds r LEFT JOIN orders o ON {join} "
            "LEFT JOIN signups s ON o.customer_id = s.customer_id GROUP BY 1",
        )
        assert got == {(row["channel"], row["goods"]) for row in gold}
        assert ("web", 0) in got
        assert "_source_rollup AS" in response["rendered_sql"]
        compiled = compile_query(config, Registry(config), query)
        source_rollups = [
            node
            for node in sql_nodes(compiled["sql_ast"])
            if isinstance(node, SqlCte) and node.name.endswith("_source_rollup")
        ]
        assert source_rollups
        for cte in source_rollups:
            names = [field.alias.casefold() for field in cte.query.select]
            assert len(names) == len(set(names))
            if composite:
                assert "m1_rows_2" in names
        assert_settled_in_one_place(compiled, config)
    finally:
        rt.close()


@pytest.mark.parametrize("op", ["add", "subtract"])
@pytest.mark.parametrize(
    ("kind", "width", "expected"),
    [
        (
            "rolling",
            3,
            {
                "2023-11": (21, 9),
                "2023-12": (28, 16),
                "2024-01": (48, 36),
                "2024-02": (27, 27),
                "2024-03": (36, 28),
            },
        ),
        ("rolling", 1, {"2024-01": (None, None), "2024-02": (0, 0)}),
        ("cumulative", 0, {"2024-01": (48, 36)}),
        ("period_to_date", 0, {"2024-01": (None, None), "2024-03": (36, 28)}),
    ],
    ids=["trailing_three", "trailing_one", "cumulative", "year_to_date"],
)
def test_a_summing_window_combines_each_operands_window(
    shop: Runtime,
    op: str,
    kind: str,
    width: int,
    expected: dict[str, tuple[int | None, int | None]],
) -> None:
    expression = {
        "kind": kind,
        "input": {"kind": "arithmetic", "op": op, "left": SHOP_REVENUE, "right": SHOP_GOODS},
    }
    if kind == "rolling":
        expression["window"] = {"unit": "month", "value": width}
    elif kind == "period_to_date":
        expression["period"] = "year"
    response = shop.query(
        {"select": _select(value=expression), "time": {**SHOP_MONTH, "fill": True}}
    )
    got = {
        row[f"{SHOP_MONTH['temporal_role']}__month"].strftime("%Y-%m"): row["value"]
        for row in typed_rows(response)
    }
    for month, values in expected.items():
        assert got[month] == values[op == "subtract"], month


NET = "metric.shop.net_revenue"
NET_HUNDREDTHS = "metric.shop.net_revenue_hundredths"
NET_INLINE = {"kind": "arithmetic", "op": "subtract", "left": SHOP_REVENUE, "right": SHOP_GOODS}
TWO = {"kind": "literal", "value": 2}
HUNDRED = {"kind": "literal", "value": 100}


@pytest.fixture(scope="module")
def shop_with_net(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """The shop with net = revenue - goods_refunded declared, and a metric built on net."""
    package = _write_variant(tmp_path_factory.mktemp("shop_net"), "utc_authored")
    config = load_package_config(str(package))
    recipes = {
        NET: NET_INLINE,
        NET_HUNDREDTHS: {"kind": "arithmetic", "op": "divide", "left": {"metric": NET}, "right": HUNDRED},
    }
    config = replace(
        config,
        metric_recipes=[
            *config.metric_recipes,
            *(
                MetricConfig(id=key, kind="derived", expression=parse_config_expression(value))
                for key, value in recipes.items()
            ),
        ],
    )
    rt = Runtime.from_config(config, source_path=str(package))
    try:
        yield rt
    finally:
        rt.close()


def _summing_window_query(kind: str, input_expr: dict[str, Any]) -> dict[str, Any]:
    expression = {"kind": kind, "input": input_expr}
    if kind == "rolling":
        expression["window"] = {"unit": "month", "value": 3}
    elif kind == "period_to_date":
        expression["period"] = "year"
    return {"select": _select(value=expression), "time": {**SHOP_MONTH, "fill": True}}


@pytest.mark.parametrize("kind", ["rolling", "cumulative", "period_to_date"])
@pytest.mark.parametrize(
    "input_expr",
    [
        {"kind": "arithmetic", "op": "multiply", "left": {"metric": NET}, "right": TWO},
        {"kind": "arithmetic", "op": "divide", "left": {"metric": NET}, "right": HUNDRED},
        {"kind": "arithmetic", "op": "add", "left": {"metric": NET}, "right": SHOP_REVENUE},
        {"kind": "ratio", "numerator": {"metric": NET}, "denominator": SHOP_REVENUE},
        {"metric": NET_HUNDREDTHS},
    ],
    ids=["times_two", "over_hundred", "plus_revenue", "ratio", "metric_over_hundred"],
)
def test_a_summing_window_refuses_a_metric_inside_its_input(
    shop_with_net: Runtime, kind: str, input_expr: dict[str, Any]
) -> None:
    """Only inline operands are windowed one by one: a metric inside the input would be windowed
    as one value, so January's unknown goods amount would drop its known revenue."""
    query = _summing_window_query(kind, input_expr)
    issue = shop_with_net.validate(query)["errors"][0]
    with pytest.raises(SemanticLayerError) as raised:
        shop_with_net.query(query)
    for code, details in [(issue["code"], issue["details"]), (raised.value.code, raised.value.details)]:
        assert code == "ROLLUP_UNSAFE"
        assert details["unsupported_construct"] == "nested_metric_window_input"
        assert details["construct"] == kind
        assert details["input"] == NET


@pytest.mark.parametrize(
    ("input_expr", "factor", "expected"),
    [
        ({"kind": "arithmetic", "op": "multiply", "left": NET_INLINE, "right": TWO}, 2, 72),
        ({"metric": NET}, 1, 36),
    ],
    ids=["inline_times_two", "whole_input_metric"],
)
def test_a_summing_window_answers_inline_operands_and_a_whole_input_metric(
    shop_with_net: Runtime, input_expr: dict[str, Any], factor: int, expected: int
) -> None:
    response = shop_with_net.query(_summing_window_query("rolling", input_expr))
    got = {
        row[f"{SHOP_MONTH['temporal_role']}__month"].strftime("%Y-%m"): row["value"]
        for row in typed_rows(response)
    }
    # November 2023 to January 2024, each part summed over the base rows on its own.
    window = "o.ordered_at >= TIMESTAMP '2023-11-01' AND o.ordered_at < TIMESTAMP '2024-02-01'"
    gold = _gold(
        shop_with_net,
        f"SELECT (SELECT SUM(o.amount) FROM orders o WHERE {window}) - (SELECT SUM(r.goods_amount) "
        f"FROM refunds r JOIN orders o ON r.order_id = o.order_id WHERE {window}) AS net",
    )
    assert factor * gold[0]["net"] == expected
    assert got["2024-01"] == expected


@pytest.mark.parametrize("fill", [False, True])
def test_a_sum_whose_rows_all_lack_a_value_is_unknown_and_its_count_is_not(
    shop: Runtime, fill: bool
) -> None:
    window = {"start": "2024-05-01", "end": "2024-07-01", "fill": fill}
    response = shop.query(
        {
            "version": 1,
            "select": _select(revenue=SHOP_REVENUE, orders=SHOP_ORDERS),
            "group_by": [SHOP_STORE],
            "time": {**SHOP_MONTH, **window},
        }
    )
    month = f"{SHOP_MONTH['temporal_role']}__month"
    got = {
        (row[SHOP_STORE], str(row[month])[:7]): (row["revenue"], row["orders"])
        for row in typed_rows(response)
    }
    expected = {("a", "2024-05"): (None, 1), (None, "2024-05"): (6, 1), ("b", "2024-06"): (3, 1)}
    if fill:  # the groups with no rows, where both measures have data in scope
        expected |= {key: (0, 0) for key in [("a", "2024-06"), ("b", "2024-05"), (None, "2024-06")]}
    assert got == expected
    # Unfilled, whole months: the rollup answers, and its row for store a's May holds no amount.
    assert ("FROM orders_monthly" in response["rendered_sql"]) is not fill


def test_an_unknown_value_meets_no_threshold_not_even_one_zero_passes(shop: Runtime) -> None:
    """A threshold 0 passes keeps every order that doesn't fail it, the ones with no rows too;
    order 7's unknown revenue is not one of them."""
    below_5 = {
        "kind": "metric_predicate",
        "entity": "entity.shop_order",
        "scope_mode": "entity_only",
        "input": SHOP_REVENUE,
        "op": "<",
        "value": 5,
    }
    response = shop.query(
        {
            "version": 1,
            "select": _select(orders=SHOP_ORDERS),
            "group_by": [SHOP_STORE],
            "metric_filters": [{"expression": below_5, "op": "=", "value": True}],
        }
    )
    got = {row[SHOP_STORE]: row["orders"] for row in typed_rows(response)}
    gold = _gold(
        shop, "SELECT store_id AS s, COUNT(*) AS n FROM orders WHERE amount < 5 GROUP BY 1"
    )
    assert got == {row["s"]: row["n"] for row in gold} == {"a": 2, "b": 1}


def test_a_threshold_zero_passes_on_a_sum_of_measures_keeps_entities_without_rows(
    shop: Runtime,
) -> None:
    """Without order 7, every order with refunds has goods or shipping amounts, never both. A
    NULL sum of the two then doesn't say either measure lacks data in scope, so the orders with
    no refunds still qualify (0 + 0 = 0); an operand's unknown amounts read 0 here."""
    no_refund = {
        "kind": "metric_predicate",
        "entity": "entity.shop_order",
        "scope_mode": "contextual",
        "input": {"kind": "arithmetic", "op": "add", "left": SHOP_GOODS, "right": SHOP_SHIPPING},
        "op": "=",
        "value": 0,
    }
    response = shop.query(
        {
            "version": 1,
            "select": _select(orders=SHOP_ORDERS),
            "group_by": [SHOP_ORDER],
            "where": [{"field": SHOP_ORDER, "op": "!=", "value": 7}],
            "metric_filters": [{"expression": no_refund, "op": "=", "value": True}],
        }
    )
    got = sorted(row[SHOP_ORDER] for row in typed_rows(response))
    gold = _gold(
        shop,
        "SELECT order_id AS id FROM orders WHERE order_id <> 7 "
        "AND order_id NOT IN (SELECT order_id FROM refunds) ORDER BY 1",
    )
    assert got == [row["id"] for row in gold] == [1, 3, 5, 8, 9, 10, 11]


@pytest.mark.parametrize("beside_a_distribution", [False, True])
@pytest.mark.parametrize("branches", [1, 2])
@pytest.mark.parametrize("else_value", ["none", None, 0])
def test_a_rollup_never_answers_a_conditional_sum(
    shop_package: Path, branches: int, else_value: int | str | None, beside_a_distribution: bool
) -> None:
    """A rollup's sum can't tell rows that all fail a CASE condition (0) from rows that meet it
    with no value (NULL), so routing leaves such a measure on the base table. A non-NULL ELSE
    reads every row, as the base path's row count does, so the rollup may answer it. Beside a
    distribution the plan keeps the earlier settlement, where the rollup answers any of them."""
    config = load_package_config(str(shop_package))
    amount = {"kind": "column", "column": "amount"}
    whens = [{"when": IN_STORE_A, "then": amount}, {"when": IN_STORE_B, "then": amount}]
    payload: dict[str, Any] = {"kind": "case", "whens": whens[:branches]}
    if else_value != "none":
        payload["else"] = {"kind": "literal", "value": else_value}
    config = replace(
        config,
        measures=[
            replace(row, expr=parse_config_expression(payload))
            if row.id == SHOP_REVENUE["measure"]
            else row
            for row in config.measures
        ],
    )
    query = {"select": _select(revenue=SHOP_REVENUE), "group_by": [SHOP_STORE], "time": SHOP_MONTH}
    if beside_a_distribution:
        query["select"] += REFUNDS_BESIDE_A_MEDIAN["select"][1:]
    compiled = compile_query(config, Registry(config), {"version": 1, **query})
    (leaf,) = compiled["logical_plan"].measure_plans
    if else_value == 0 or beside_a_distribution:
        assert leaf.aggregate_relation_id != ""
        return
    assert leaf.aggregate_relation_id == ""
    assert set(leaf.aggregate_relation_rejections.values()) == {"aggregation_not_reaggregable"}


def test_a_rollup_leaf_without_a_row_count_is_refused(
    shop_package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the bypass on the routed path: its leaf counts no rows, and the guard refuses."""
    monkeypatch.setattr(sql_lowering, "reads_every_row", lambda measure: False)
    config = load_package_config(str(shop_package))
    query = {"select": _select(revenue=SHOP_REVENUE), "group_by": [SHOP_STORE], "time": SHOP_MONTH}
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), {"version": 1, **query})
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert raised.value.details["missing"] == "row_count"


# -- ClickHouse reads an unmatched outer-join field as NULL only when told to ---------------


@pytest.mark.parametrize("warehouse", ["duckdb", "postgres", "clickhouse", "snowflake"])
def test_clickhouse_statements_set_join_use_nulls(config: Any, warehouse: str) -> None:
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    query = {"version": 2, "select": _select(revenue=REVENUE, items=ITEMS), "group_by": [STORE]}
    sql = compile_query(config, Registry(config), query)["sql"]
    assert sql.endswith("\nSETTINGS join_use_nulls = 1") is (warehouse == "clickhouse")


def test_a_plain_time_leaf_cannot_bypass_scope_recording(config, monkeypatch):
    monkeypatch.setattr(sql_lowering, "record_leaf_scope", lambda *args: None)
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(
            config,
            Registry(config),
            {
                "select": _select(revenue=REVENUE),
                "time": {
                    "temporal_role": ORDER_TIME,
                    "grain": "month",
                    "start": "2017-04-01",
                    "end": "2017-05-01",
                },
            },
        )
    assert caught.value.code == "EMPTY_GROUPS_UNSETTLED"
