"""Empty groups: NULL when there is no data, 0 when there is data of nothing.

A group whose rows exist but whose values are all NULL has no data for that sum: NULL.

Gold values come from raw SQL on the seeded tables, or from the shop's few orders where they
are named. The differential corpus in ``tests/integration/correctness`` holds the same rule to
independent SQL on DuckDB and Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from semantic_rails import compiler
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import (
    distribution,
    leaves,
    lowering_common,
    planning,
    row_counts,
    sql_lowering,
    time_series,
)
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
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.empty_groups_invariant import assert_settled_in_one_place
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_rendered_sql_snapshots import SNAPSHOT_CASES

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
ORDER_ID = "dimension.jaffle_order_id"
REVENUE = {"kind": "measure", "measure": "measure.jaffle.revenue_usd"}
ORDERS = {"kind": "measure", "measure": "measure.jaffle.order_count"}
ITEMS = {"kind": "measure", "measure": "measure.jaffle.item_count"}
NO_SUCH_STORE = [{"field": STORE, "op": "is", "value": "No such store"}]


def _select(**expressions: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"expression": expression, "as": alias} for alias, expression in expressions.items()]


def _only(measure: dict[str, Any], where: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """``measure`` aggregated over the rows ``where`` keeps."""
    return {**measure, "kind": "aggregate", "filter": {"all": where}, **extra}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """One runtime for the module (every query here only reads), so the data is seeded once."""
    package_dir = copy_package_config(tmp_path_factory.mktemp("empty_groups"), "jaffle_shop")
    package = load_package_config(str(package_dir))
    rt = Runtime.from_config(package, source_path=str(package_dir), package_id="jaffle_shop")
    try:
        yield opened(rt)
    finally:
        rt.close()


@pytest.fixture(scope="module")
def config() -> Any:
    return load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))


def _gold(runtime: Runtime, sql: str) -> list[dict[str, Any]]:
    """Run raw SQL on the runtime's own database, bypassing the compiler."""
    return list(runtime._get_adapter().query(sql))


def _warnings(response: dict[str, Any], code: str = "NO_DATA_IN_SCOPE") -> list[dict[str, Any]]:
    return [item for item in response.get("warnings", []) if item.get("code") == code]


# -- the predicate -------------------------------------------------------------------------


RESOLVES_TO_ZERO = {
    ("sum", "additive", True): True,
    ("count", "event_count", True): True,
    ("count_distinct", "entity_count", True): True,
    ("", "additive", True): True,  # the measure's own default aggregation is a sum
    ("avg", "additive", True): False,
    ("min", "additive", True): False,
    ("max", "additive", True): False,
    ("median", "additive", True): False,
    ("sum", "semi_additive", True): False,  # a stock has no value for nothing
    ("count_distinct", "distinct_population", True): False,
    ("sum", "additive", False): False,  # already-aggregated values are never made up
}


def test_only_sums_and_counts_of_additive_measures_resolve_to_zero(config: Any) -> None:
    revenue = next(row for row in config.measures if row.id == "measure.jaffle.revenue_usd")
    got = {
        (aggregation, measure_class, additive): resolves_to_zero(
            aggregation,
            replace(
                revenue, measure_class=measure_class, additive=additive, default_aggregation="sum"
            ),
        )
        for aggregation, measure_class, additive in RESOLVES_TO_ZERO
    }
    assert got == RESOLVES_TO_ZERO
    assert not any(resolves_to_zero(aggregation, None) for aggregation, _, _ in RESOLVES_TO_ZERO)


# -- 0 where the measure has data elsewhere, NULL where it has none -----------------------


def test_a_count_beside_a_second_fact_reads_zero_in_its_empty_group(runtime: Runtime) -> None:
    window = {"start": "2017-04-01", "end": "2017-04-08"}
    response = runtime.query(
        {
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


def test_a_limit_and_a_result_filter_cannot_change_what_the_guard_sees(runtime: Runtime) -> None:
    """The orders with no items are all-zero once filtered, but the measure has items elsewhere."""
    unlimited_query = {
        "select": _select(orders=ORDERS),
        "group_by": [ORDER_ID],
        "having": [{"field": ITEMS["measure"], "op": "is", "value": 0}],
    }
    response = runtime.query({**unlimited_query, "limit": 5})
    assert response["row_count"] == 5
    assert not _warnings(response)
    # The settled value, not a NULL turned into 0 after the filter: the same orders with no
    # limit are the gold orders that have no items, each with an order count of 1.
    unlimited = runtime.query(unlimited_query)
    gold = _gold(
        runtime,
        "SELECT o.order_id AS id FROM jaffle_order o "
        "WHERE NOT EXISTS (SELECT 1 FROM jaffle_item i WHERE i.order_id = o.order_id)",
    )
    assert {row[ORDER_ID] for row in unlimited["rows"]} == {row["id"] for row in gold}
    assert all(row["orders"] == 1 for row in unlimited["rows"] + response["rows"])
    assert {row[ORDER_ID] for row in response["rows"]} <= {row["id"] for row in gold}


def test_a_filter_that_matches_nothing_reads_null_and_says_so(runtime: Runtime) -> None:
    """Under the query scope; the dataset scope reads 0, as the raw count of no rows does, and
    names the value that matched nothing (test_observation_scope)."""
    query = {
        "select": _select(revenue=REVENUE, orders=ORDERS),
        "where": NO_SUCH_STORE,
        "observation_scope": "query",
    }
    response = runtime.query(query)
    assert response["rows"] == [{"revenue": None, "orders": None}]
    (warning,) = _warnings(response)
    assert warning["details"]["outputs"] == ["revenue", "orders"]
    assert warning["severity"] == "warning"
    assert not _warnings(response, "FILTER_VALUE_NOT_FOUND")
    dataset = runtime.query({**query, "observation_scope": "dataset"})
    assert dataset["rows"] == [{"revenue": 0, "orders": 0}]
    assert not _warnings(dataset)
    (typo,) = _warnings(dataset, "FILTER_VALUE_NOT_FOUND")
    assert typo["details"]["filters"] == [
        {"dimension": STORE, "value": "No such store", "suggestion": None}
    ]


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_a_never_matched_authored_filter_reads_zero_in_observed_buckets(
    runtime: Runtime, scope: str
) -> None:
    quarters = {"temporal_role": ORDER_TIME, "grain": "quarter"}
    response = runtime.query(
        {
            "select": _select(revenue=REVENUE, none=_only(REVENUE, NO_SUCH_STORE)),
            "observation_scope": scope,
            "time": quarters,
        }
    )
    assert response["row_count"] > 1
    gold = _gold(
        runtime,
        "SELECT date_trunc('quarter', o.ordered_at) AS bucket, SUM(o.order_total_cents) / 100.0 AS revenue, "
        "SUM(CASE WHEN s.store_name = 'No such store' THEN o.order_total_cents / 100.0 ELSE 0 END) AS none "
        "FROM jaffle_order o LEFT JOIN jaffle_store s ON s.store_id = o.store_id GROUP BY 1 ORDER BY 1",
    )
    assert [(row["revenue"], row["none"]) for row in response["rows"]] == [
        (pytest.approx(row["revenue"]), row["none"]) for row in gold
    ]
    assert not _warnings(response)
    (warning,) = _warnings(response, "FILTER_VALUE_NOT_FOUND")
    assert warning["details"]["filters"][0]["value"] == NO_SUCH_STORE[0]["value"]
    philadelphia = _only(REVENUE, [{"field": STORE, "op": "is", "value": "Philadelphia"}])
    real = runtime.query(
        {
            "select": _select(revenue=REVENUE, filtered=philadelphia),
            "observation_scope": scope,
            "time": quarters,
        }
    )
    assert not _warnings(real, "FILTER_VALUE_NOT_FOUND")
    assert not _warnings(real, "FILTER_VALUE_UNVERIFIED")


@pytest.mark.parametrize(
    ("query", "rows"),
    [
        # An average of nothing is undefined, not missing data.
        (
            {"select": _select(average=_only(REVENUE, NO_SUCH_STORE, aggregation="avg"))},
            [{"average": None}],
        ),
        # A result filter that removes every group.
        (
            {
                "select": _select(revenue=REVENUE),
                "group_by": [STORE],
                "having": [{"field": ORDERS["measure"], "op": "gt", "value": 100000}],
            },
            [],
        ),
    ],
    ids=["average_of_nothing", "result_filter_removes_every_group"],
)
def test_an_answer_with_data_in_scope_is_not_missing_data(
    runtime: Runtime, query: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    response = runtime.query(query)
    assert response["rows"] == rows
    assert not _warnings(response)


def test_no_rows_and_no_time_window_says_nothing_matched(runtime: Runtime) -> None:
    query = {
        "select": _select(revenue=REVENUE),
        "group_by": [STORE],
        "where": NO_SUCH_STORE,
        "observation_scope": "query",
    }
    response = runtime.query(query)
    assert response["rows"] == []
    assert [item["details"]["outputs"] for item in _warnings(response)] == [["revenue"]]
    # Judged across the dataset, no rows is the filter's: revenue has data elsewhere.
    dataset = runtime.query({**query, "observation_scope": "dataset"})
    assert dataset["rows"] == []
    assert not _warnings(dataset)
    assert _warnings(dataset, "FILTER_VALUE_NOT_FOUND")
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
            "select": _select(orders=ORDERS, prior_year=prior),
            "time": {"temporal_role": ORDER_TIME, "grain": "month", "end": "2017-06-01"},
        }
    )
    assert 1 < response["row_count"] <= 12
    assert all(row["orders"] > 0 and row["prior_year"] is None for row in response["rows"])
    assert not _warnings(response)


@pytest.mark.parametrize(
    ("scope", "code"), [("query", "NO_DATA_IN_SCOPE"), ("dataset", "FILTER_VALUE_NOT_FOUND")]
)
def test_the_query_mcp_carries_the_warning_at_its_default_verbosity(
    runtime: Runtime, scope: str, code: str
) -> None:
    query = {"select": _select(revenue=REVENUE), "where": NO_SUCH_STORE}
    query["observation_scope"] = scope
    response = SemanticLayerMCPAdapter(runtime).call_tool("execute", {"query": query})
    assert response["ok"], response["errors"]
    assert [item["code"] for item in _warnings(response, code)] == [code]


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


# -- one place settles them ----------------------------------------------------------------


def _customers_where(input_: dict[str, Any], op: str, value: int) -> list[dict[str, Any]]:
    """A metric filter keeping the customers whose own ``input`` meets ``op value``."""
    predicate = {
        "kind": "metric_predicate",
        "entity": "entity.jaffle_customer",
        "scope_mode": "entity_only",
        "input": input_,
        "op": op,
        "value": value,
    }
    return [{"expression": predicate, "op": "=", "value": True}]


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
    "filtered_by_store": {
        "select": _select(revenue=REVENUE, orders=ORDERS, items=ITEMS),
        "group_by": [ORDER_ID],
        "where": [{"field": STORE, "op": "is", "value": "Philadelphia"}],
        "time": {"temporal_role": ORDER_TIME, "start": "2017-04-01", "end": "2017-04-08"},
    },
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
        "metric_filters": _customers_where(
            {"kind": "measure", "measure": "measure.jaffle.large_order_count"}, "=", 0
        ),
    },
    "predicate_zero_passes": {
        "select": _select(revenue=REVENUE),
        "group_by": [STORE],
        "metric_filters": _customers_where(
            {
                "kind": "arithmetic",
                "op": "subtract",
                "left": ORDERS,
                "right": _only(ORDERS, NO_SUCH_STORE),
            },
            "<",
            1,
        ),
    },
}


@pytest.mark.parametrize("shape", SHAPES)
def test_every_sum_and_count_a_projection_reads_comes_from_the_guard(
    config: Any, shape: str
) -> None:
    compiled = compile_query(config, Registry(config), {"version": 1, **SHAPES[shape]})
    assert_settled_in_one_place(compiled, config)


# -- rows whose values are all unknown are not a group with no rows -------------------------
# The differential corpus's shop: order 7 is store a's only order in May 2024 and has no
# amount; order 8 (May) has no store; store b's only order of May and June is order 9 (June).

SHOP_ORDER = "dimension.shop_order_id"
SHOP_STORE = "dimension.shop_order_store_id"
SHOP_ROLE = "temporal_role.shop_order_ordered_at"
SHOP_MONTH = {"temporal_role": SHOP_ROLE, "grain": "month"}
SHOP_REVENUE = {"kind": "measure", "measure": "measure.shop.revenue"}
SHOP_ORDERS = {"kind": "measure", "measure": "measure.shop.order_count"}
SHOP_GOODS = {"kind": "measure", "measure": "measure.shop.goods_refunded"}
SHOP_SHIPPING = {"kind": "measure", "measure": "measure.shop.shipping_refunded"}
STORE_A_ORDERS = {"kind": "measure", "measure": "measure.shop.store_a_orders"}
STORE_B_ORDERS = {"kind": "measure", "measure": "measure.shop.store_b_orders"}
HUGE_ORDERS = {"kind": "measure", "measure": "measure.shop.huge_order_count"}
AMOUNT = {"kind": "column", "column": "amount"}
IN_STORE_A = {
    "kind": "comparison",
    "op": "=",
    "left": {"kind": "column", "column": "store_id", "entity": "entity.shop_order"},
    "right": {"kind": "literal", "value": "a"},
}
IN_STORE_B = {**IN_STORE_A, "right": {"kind": "literal", "value": "b"}}
# The week before 2023-12-04, whose only order is order 3 (store a), and that instant.
LAST_WEEK = {"temporal_role": SHOP_ROLE, "start": "2023-11-27", "end": "2023-12-04"}
WEEK_END = "2023-12-04T00:00:00+00:00"
# The same week written with explicit bounds and as the last complete week.
LAST_WEEK_SPELLINGS = [
    {"start": "2023-11-27", "end": "2023-12-04"},
    {"last": {"unit": "week", "value": 1}},
]
A_B_NET = _select(
    a=STORE_A_ORDERS,
    b=STORE_B_ORDERS,
    net={"kind": "arithmetic", "op": "subtract", "left": STORE_A_ORDERS, "right": STORE_B_ORDERS},
)


def _a_b_net(response: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [(row["a"], row["b"], row["net"]) for row in typed_rows(response)]


def _amount_when(*conditions: dict[str, Any], otherwise: Any = "none") -> dict[str, Any]:
    """CASE WHEN a condition THEN amount, with ELSE ``otherwise`` unless that is "none"."""
    case: dict[str, Any] = {
        "kind": "case",
        "whens": [{"when": c, "then": AMOUNT} for c in conditions],
    }
    if otherwise != "none":
        case["else"] = {"kind": "literal", "value": otherwise}
    return case


def _amount_if(condition: dict[str, Any], column: str = "amount") -> dict[str, Any]:
    value = {"kind": "column", "column": column, "entity": "entity.shop_order"}
    return {"kind": "aggregate_if", "aggregation": "sum", "condition": condition, "value": value}


def _refund_type_is(value: str) -> dict[str, Any]:
    return {
        "kind": "comparison",
        "op": "=",
        "left": {"kind": "column", "column": "refund_type"},
        "right": {"kind": "literal", "value": value},
    }


def _with_measure(
    config: Any, payload: dict[str, Any], measure: str = SHOP_REVENUE["measure"], **changes: Any
) -> Any:
    """``config`` with ``measure`` computed as ``payload`` (and any other ``changes``)."""
    expr = parse_config_expression(payload)
    measures = [replace(row, expr=expr) if row.id == measure else row for row in config.measures]
    return replace(config, measures=measures, **changes)


def _conditional_revenue(rt: Runtime, condition: dict[str, Any]) -> Runtime:
    """The runtime's package with revenue summed only where ``condition`` holds."""
    config = _with_measure(rt.config, _amount_when(condition))
    return Runtime.from_config(config, source_path=rt.source_path)


def _shop_package(root: Path, *statements: str) -> Path:
    """The shop, with ``statements`` run after its own seed."""
    package = _write_variant(root, "utc_authored")
    seed = package / "data" / "seed.sql"
    extra = "".join(f"\n{statement};" for statement in statements)
    seed.write_text(seed.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return package


@pytest.fixture(scope="module")
def shop_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_variant(tmp_path_factory.mktemp("shop"), "utc_authored")


@pytest.fixture(scope="module")
def shop(shop_package: Path) -> Iterator[Runtime]:
    rt = Runtime.from_path(str(shop_package))
    try:
        yield opened(rt)
    finally:
        rt.close()


def test_distribution_sum_keeps_a_measured_else_zero(shop: Runtime) -> None:
    amount = {"kind": "measure", "measure": "measure.shop.store_a_amount"}
    distribution = {
        "kind": "distribution",
        "function": "sum",
        "over": {"kind": "entity_value", "entity": "entity.shop_customer", "input": amount},
    }
    query = {
        "select": _select(total=distribution, amount=amount),
        "time": {**SHOP_MONTH, "grain": "week", "start": "2023-11-20", "end": "2023-11-27"},
    }
    # A named window fills the combined answer, where the amount's empty week can't tell
    # 0 from no data yet: the two are asked for separately.
    with pytest.raises(SemanticLayerError) as refused:
        shop.query(query)
    assert refused.value.details["reason"] == "distribution_beside_additive_in_named_window"
    alone = shop.query({**query, "select": _select(amount=amount)})
    assert typed_rows(alone)[0]["amount"] == 0
    # The week's only order is store b's: its customer's store-a amount is a measured 0.
    response = shop.query({**query, "select": _select(total=distribution)})
    assert [row["total"] for row in typed_rows(response)] == [0]
    assert not _warnings(response)


@pytest.mark.parametrize(
    ("grain", "expected"),
    [
        # The total: a total with data says nothing about missing data.
        (None, [(1, 0, 1)]),
        ("week", [(1, 0, 1)]),
        # Each month the window touches: no order in its November days, order 3 in December.
        ("month", [(0, 0, 0), (1, 0, 1)]),
    ],
)
def test_a_window_total_observes_conditional_counts_outside_its_bounds(
    shop: Runtime, grain: str | None, expected: list[tuple[int, int, int]]
) -> None:
    for time in LAST_WEEK_SPELLINGS:
        bucket = {"grain": grain} if grain else {}
        response = shop.query(
            {"select": A_B_NET, "time": {"temporal_role": SHOP_ROLE, **bucket, **time}},
            now=WEEK_END,
        )
        assert _a_b_net(response) == expected, time
        assert not _warnings(response)


@pytest.mark.parametrize("grain", [None, "week"])
def test_a_window_count_never_matched_reads_zero_where_its_bucket_is_checked(
    shop: Runtime, grain: str | None
) -> None:
    """A named window's buckets, its total's or its weeks', get the loaded-bucket check, so a
    count that never matched (no order reaches 1000) reads 0 where the data is loaded."""
    time = {**LAST_WEEK, **({"grain": grain} if grain else {})}
    response = shop.query({"select": _select(huge=HUGE_ORDERS), "time": time})
    assert [row["huge"] for row in response["rows"]] == [0]
    assert not _warnings(response)


# Store a's three orders in the week before 2023-12-04, none with an amount; no store b order.
ONLY_A = (
    "DELETE FROM orders",
    "INSERT INTO orders (order_id, customer_id, store_id, ordered_at, amount) VALUES "
    "(21, 101, 'a', TIMESTAMP '2023-11-28', NULL), (22, 101, 'a', TIMESTAMP '2023-11-29', NULL), "
    "(23, 101, 'a', TIMESTAMP '2023-11-30', NULL)",
)


@pytest.fixture(scope="module")
def only_a_shop(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    rt = Runtime.from_path(str(_shop_package(tmp_path_factory.mktemp("only_a"), *ONLY_A)))
    try:
        yield opened(rt)
    finally:
        rt.close()


def test_a_never_matched_count_preserves_the_window_difference(only_a_shop: Runtime) -> None:
    # The named window's week, like its total, gets the loaded-bucket check: b reads 0.
    for time in [
        {**bounds, **grain} for bounds in LAST_WEEK_SPELLINGS for grain in ({}, {"grain": "week"})
    ]:
        response = only_a_shop.query(
            {"select": A_B_NET, "time": {"temporal_role": SHOP_ROLE, **time}}, now=WEEK_END
        )
        assert _a_b_net(response) == [(3, 0, 3)], time
        assert not _warnings(response)


def test_a_window_total_without_its_loaded_bucket_check_never_settles_from_source_rows(
    only_a_shop: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the bypass: lowering emits no loaded-bucket check for the total. The guard then
    can't settle the never-matched operand from source rows, so it holds NULL and warns."""
    for module in (lowering_common, sql_lowering, time_series):
        monkeypatch.setattr(module, "_emits_time_coverage", lambda plan, config: False)
    # A runtime of its own, so no compilation cached before the patch is reused.
    rt = Runtime.from_config(only_a_shop.config, source_path=only_a_shop.source_path)
    try:
        response = rt.query({"select": A_B_NET, "time": LAST_WEEK}, now=WEEK_END)
    finally:
        rt.close()
    assert "coverage_1" not in response["rendered_sql"]
    assert _a_b_net(response) == [(3, None, None)]
    assert _warnings(response)[0]["details"]["outputs"] == ["b", "net"]


@pytest.mark.parametrize(
    ("package", "shape", "condition", "expected"),
    [
        # Store a's orders have no amount: unknown. Store b has no order: 0.
        ("only_a_shop", "case", IN_STORE_A, None),
        ("only_a_shop", "case", IN_STORE_B, 0),
        ("only_a_shop", "aggregate_if", IN_STORE_A, None),
        ("only_a_shop", "aggregate_if", IN_STORE_B, 0),
        # Store b's orders elsewhere have amounts; none falls in the week.
        ("shop", "case", IN_STORE_B, 0),
    ],
)
def test_a_conditional_window_sum_distinguishes_no_match_from_unknown_amounts(
    request: pytest.FixtureRequest,
    package: str,
    shape: str,
    condition: dict[str, Any],
    expected: int | None,
) -> None:
    base = request.getfixturevalue(package)
    if shape == "case":
        rt = _conditional_revenue(base, condition)
        query = {"select": _select(total=SHOP_REVENUE), "time": LAST_WEEK}
    else:  # every order of this shop is in the week
        rt = Runtime.from_config(base.config, source_path=base.source_path)
        where = [{"field": SHOP_STORE, "op": "is", "value": "a"}]
        query = {"select": _select(total=_amount_if(condition)), "where": where}
    try:
        response = rt.query(query)
    finally:
        rt.close()
    assert [row["total"] for row in typed_rows(response)] == [expected]
    if expected == 0:
        assert not _warnings(response)


@pytest.mark.parametrize(("scope", "expected"), [("dataset", 0), ("query", None)])
def test_a_never_matched_count_respects_the_query_observation_scope(
    only_a_shop: Runtime, scope: str, expected: int | None
) -> None:
    # No order has store 'missing': judged inside that filter, store b's count has no data.
    response = only_a_shop.query(
        {
            "select": _select(b=STORE_B_ORDERS),
            "where": [{"field": SHOP_STORE, "op": "is", "value": "missing"}],
            "observation_scope": scope,
        }
    )
    assert [row["b"] for row in typed_rows(response)] == [expected]
    assert bool(_warnings(response)) is (expected is None)


@pytest.mark.parametrize("population", ["empty", "outside"])
def test_a_never_matched_count_requires_a_loaded_relation(tmp_path: Path, population: str) -> None:
    """With no orders at all, or none in the window's month (October 2023, before the data), a
    count that never matched has no data: NULL."""
    rt = Runtime.from_path(
        str(_shop_package(tmp_path, *(["DELETE FROM orders"] if population == "empty" else [])))
    )
    query: dict[str, Any] = {"select": _select(huge=HUGE_ORDERS)}
    if population == "outside":
        # A named window answers every period of its grain.
        query["time"] = {**SHOP_MONTH, "start": "2023-10-01", "end": "2023-11-01"}
    try:
        response = rt.query(query)
    finally:
        rt.close()
    assert response["rows"]
    assert all(row["huge"] is None for row in response["rows"])
    assert _warnings(response)[0]["details"]["outputs"] == ["huge"]


@pytest.fixture(scope="module")
def future_shop(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """Store a's three November 2023 orders and a placeholder order dated 2098; no store b
    order anywhere. The calendar covers January 2098, so a filled series can show it."""
    package = _shop_package(
        tmp_path_factory.mktemp("future"),
        *ONLY_A,
        "INSERT INTO orders (order_id, customer_id, store_id, ordered_at, amount) VALUES "
        "(24, 101, 'a', TIMESTAMP '2098-01-15', NULL)",
        "INSERT INTO dim_date SELECT CAST(d AS DATE), CAST(date_trunc('week', d) AS DATE), "
        "CAST(date_trunc('month', d) AS DATE), CAST(date_trunc('quarter', d) AS DATE), "
        "CAST(date_trunc('year', d) AS DATE) FROM generate_series(TIMESTAMP '2098-01-01', "
        "TIMESTAMP '2098-01-31', INTERVAL '1 day') AS g(d)",
    )
    rt = Runtime.from_path(str(package))
    try:
        yield opened(rt)
    finally:
        rt.close()


@pytest.mark.parametrize("shape", ["count", "sum"])
def test_a_never_matched_operand_reads_null_beyond_the_loaded_range(
    future_shop: Runtime, shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Store b's operand in January 2098, past the last loaded month (November 2023, the last
    with a timestamp that isn't in the future), has no data yet: NULL. The dataset probe ignores
    the query's window as well as its filters, so it never settles a bucket the loaded-bucket
    check doesn't gate."""
    rt = _conditional_revenue(future_shop, IN_STORE_B) if shape == "sum" else future_shop
    b = SHOP_REVENUE if shape == "sum" else STORE_B_ORDERS
    # A named window answers every period of its grain.
    query = {
        "select": _select(b=b),
        "time": {**SHOP_MONTH, "start": "2098-01-01", "end": "2098-02-01"},
    }
    try:
        now = "2026-10-01T00:00:00Z"  # One instant: both answers' SQL reads it.
        response = rt.query(query, now=now)
        with monkeypatch.context() as patch:
            patch.setattr(
                "semantic_rails.runtime_parts.answer_warnings._no_data_yet_warnings",
                lambda *a, **k: [],
            )
            undisclosed = rt.query(query, now=now)
        where = [{"field": SHOP_STORE, "op": "is", "value": "a"}]
        dataset = rt.query({**query, "where": where, "observation_scope": "dataset"})
    finally:
        if rt is not future_shop:
            rt.close()
    for answer in (response, dataset):
        assert [row["b"] for row in typed_rows(answer)] == [None]
        assert _warnings(answer, "NO_DATA_YET")[0]["details"]["outputs"] == ["b"]
    assert (response["rows"], response["rendered_sql"]) == (
        undisclosed["rows"],
        undisclosed["rendered_sql"],
    )
    assert _warnings(undisclosed)[0]["details"]["outputs"] == ["b"]
    # The filled series and the coverage marker bucket the same timestamps the same way; the
    # edge is the last order's day, November 30.
    assert _warnings(response) == []
    [warning] = _warnings(response, "NO_DATA_YET")
    assert warning["details"]["measures"] == [
        {"id": b["measure"], "edge": "2023-11-30", "edge_source": "last_row"}
    ]


@pytest.mark.parametrize("refunds", ["empty", "populated"])
def test_a_window_total_operand_from_another_relation_needs_its_own_rows(
    tmp_path: Path, refunds: str
) -> None:
    """Store a's orders come from one relation and the conditional refund count from another.
    With no refund rows, b and the difference read NULL. With refund rows, the refund leaf reads
    the order clock through a join, so no loaded-bucket check gates it and a condition that
    never matched still reads NULL rather than 0."""
    package = _shop_package(tmp_path, *(["DELETE FROM refunds"] if refunds == "empty" else []))
    store_credit = {
        "when": _refund_type_is("store credit"),
        "then": {"kind": "column", "column": "refund_id"},
    }
    refunds_measure = "measure.shop.refund_count"
    config = _with_measure(
        load_package_config(str(package)),
        {"kind": "case", "whens": [store_credit]},
        refunds_measure,
    )
    b = {"kind": "measure", "measure": refunds_measure}
    select = _select(
        a=STORE_A_ORDERS,
        b=b,
        net={"kind": "arithmetic", "op": "subtract", "left": STORE_A_ORDERS, "right": b},
    )
    rt = Runtime.from_config(config, source_path=str(package))
    try:
        response = rt.query({"select": select, "time": LAST_WEEK}, now=WEEK_END)
    finally:
        rt.close()
    # Order 3 is store a's in the week, and no refund is store credit. With refund rows the
    # reference reads b as 0, but that needs the loaded-bucket check this leaf doesn't get, so
    # the never-matched operand is held at NULL, with the warning, rather than settled.
    assert _a_b_net(response) == [(1, None, None)]
    assert _warnings(response)[0]["details"]["outputs"] == ["b", "net"]


def test_a_predicate_over_several_measures_keeps_explicit_else_zero(shop: Runtime) -> None:
    # An entity filter names one measure or metric; the compiler's own form takes an inline
    # combination of several.
    combined = {
        "kind": "arithmetic",
        "op": "add",
        "left": {"kind": "measure", "measure": "measure.shop.store_a_amount"},
        "right": SHOP_ORDERS,
    }
    predicate = {
        "kind": "metric_predicate",
        "entity": "entity.shop_customer",
        "scope_mode": "entity_only",
        "time_alignment": "query_window",
        "input": combined,
        "op": "<=",
        "value": 1,
    }
    response = shop.query_internal(
        {
            "select": _select(orders=SHOP_ORDERS),
            "time": {**SHOP_MONTH, "grain": "week", "start": "2023-11-20", "end": "2023-11-27"},
            "metric_filters": [{"expression": predicate, "op": "=", "value": True}],
        }
    )
    # Order 2 is the week's only order: its customer's store-a amount is a measured 0, plus
    # one order, meets the threshold.
    assert [row["orders"] for row in response["rows"]] == [1]


@pytest.mark.parametrize("beside_distribution", [False, True])
def test_an_exact_rollup_of_an_else_zero_sum_matches_the_base(
    tmp_path: Path, beside_distribution: bool
) -> None:
    shop_package = _shop_package(
        tmp_path,
        "CREATE OR REPLACE TABLE orders_monthly AS SELECT CAST(date_trunc('month', ordered_at) "
        "AS DATE) AS month_start, store_id, SUM(amount) AS revenue, COUNT(order_id) AS order_count, "
        "SUM(CASE WHEN store_id = 'a' THEN amount ELSE 0 END) AS store_a_amount "
        "FROM orders GROUP BY 1, 2",
    )
    orders = shop_package / "models" / "orders.yml"
    model = yaml.safe_load(orders.read_text(encoding="utf-8"))
    model["model"]["variants"]["monthly"]["columns"]["store_a_amount"] = "store_a_amount"
    orders.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    config = load_package_config(str(shop_package))
    amount = {"kind": "measure", "measure": "measure.shop.store_a_amount"}
    query = {"select": _select(amount=amount), "group_by": [SHOP_STORE], "time": SHOP_MONTH}
    if beside_distribution:
        query["select"] += REFUNDS_BESIDE_A_MEDIAN["select"][1:]
    results = []
    for current in (config, replace(config, aggregate_relations=[])):
        rt = Runtime.from_config(current, source_path=str(shop_package))
        try:
            response = rt.query(query)
            assert ("FROM orders_monthly" in response["rendered_sql"]) is bool(
                current.aggregate_relations
            )
            results.append(
                {
                    (row[SHOP_STORE], str(row["period"])[:7]): row["amount"]
                    for row in typed_rows(response)
                }
            )
            gold = _gold(
                rt,
                "SELECT store_id AS s, date_trunc('month', ordered_at) AS month, "
                "SUM(CASE WHEN store_id = 'a' THEN amount ELSE 0 END) AS amount FROM orders GROUP BY 1, 2",
            )
            # A distribution retains earlier settlement for unknown amounts, independently of ELSE.
            assert results[-1] == {
                (row["s"], str(row["month"])[:7]): (
                    0 if row["amount"] is None and beside_distribution else row["amount"]
                )
                for row in gold
            }
        finally:
            rt.close()
    assert results[0] == results[1]


@pytest.mark.parametrize(
    ("revenue_alias", "median_alias"),
    [("revenue", "AGENT_BRANCH_1__ROWS"), ("agent_branch_1__rows", "agent_branch_1__rows_2")],
)
def test_branch_row_markers_never_shadow_projected_values(
    shop: Runtime, revenue_alias: str, median_alias: str
) -> None:
    """Outputs named like a branch's former hidden markers keep their values. Beside a
    distribution the earlier settlement applies: store a's May, whose only amount is unknown,
    reads 0 where revenue has data in scope."""
    distribution = {
        "kind": "distribution",
        "function": "median",
        "over": {"kind": "entity_value", "entity": "entity.shop_order", "input": SHOP_REVENUE},
    }
    for by_store, (key, values) in [
        (False, ((None, "2023-11"), (15, 7.5))),
        (True, (("a", "2024-05"), (0, None))),
    ]:
        response = shop.query(
            {
                "select": _select(**{revenue_alias: SHOP_REVENUE, median_alias: distribution}),
                "group_by": [SHOP_STORE] if by_store else [],
                "time": SHOP_MONTH,
            }
        )
        got = {
            (row.get(SHOP_STORE), str(row["period"])[:7]): (row[revenue_alias], row[median_alias])
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
        assert got[key] == values


# Goods plus shipping refunds beside the median order, without order 7: each refunded order
# has goods or shipping, never both, so every refund group reads one unknown operand.
REFUNDS_BESIDE_A_MEDIAN = {
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
    "where": [{"field": SHOP_ORDER, "op": "not", "value": 7}],
    # Beside a distribution the dataset scope refuses a where filter (test_observation_scope).
    "observation_scope": "query",
}


def test_arithmetic_beside_a_distribution_keeps_its_empty_groups_zero(shop: Runtime) -> None:
    """Beside a distribution, every branch keeps the earlier settlement: an operand's unknown
    amounts read 0 where its measure has data in scope, so a group with no refunds at all
    (store a's November) is 0, not NULL."""
    response = shop.query(REFUNDS_BESIDE_A_MEDIAN)
    got = {(row[SHOP_STORE], str(row["period"])[:7]): row["total"] for row in typed_rows(response)}
    assert got[("a", "2023-11")] == 0
    # Orders 2 (goods 5 + 1), 4 (shipping 3) and 6 (goods 4); every other group has none.
    refunded = {("b", "2023-11"): 6, ("a", "2024-01"): 3, ("a", "2024-03"): 4}
    assert got == {key: refunded.get(key, 0) for key in got}
    assert len(got) == 10


def test_an_output_named_like_a_row_count_keeps_its_name_in_order_by(shop: Runtime) -> None:
    """The leaf's hidden row count has a name of its own; a caller's alias like the measure's
    leaf alias plus ``__rows`` is the caller's, so ordering by it orders by the output."""
    alias = "leaf__measure_shop_revenue__sum__rows"
    response = shop.query(
        {
            "select": _select(**{alias: SHOP_REVENUE}),
            "group_by": [SHOP_STORE],
            "order_by": [{"field": alias, "direction": "desc"}],
        }
    )
    got = [(row[SHOP_STORE], row[alias]) for row in typed_rows(response)]
    assert got == [("a", 43), ("b", 25), (None, 6)]


RENAMED_AMOUNT = "leaf__measure_shop_revenue__sum__rows"


@pytest.mark.parametrize("extra", ["", "unrelated", "read"])
def test_a_physical_column_named_like_a_row_count_is_read_as_itself(
    tmp_path: Path, extra: str
) -> None:
    """No rewrite reaches a physical column: revenue reads its own column, whatever it is
    named, and never ``m1_rows``. The hidden row count takes a name no column the query reads
    has, so beside a sum of ``m1_rows`` (100 on every order) it is ``m1_rows_2``."""
    statements = [f"ALTER TABLE orders RENAME COLUMN amount TO {RENAMED_AMOUNT}"]
    if extra:
        statements += [
            "ALTER TABLE orders ADD COLUMN m1_rows INTEGER",
            "UPDATE orders SET m1_rows = 100",
        ]
    package = _shop_package(tmp_path, *statements)
    column = {"kind": "column", "column": RENAMED_AMOUNT}
    config = _with_measure(load_package_config(str(package)), column, aggregate_relations=[])
    rt = Runtime.from_config(config, source_path=str(package))
    try:
        every_m1_rows = _amount_if({**IN_STORE_A, "op": "!="}, "m1_rows")
        items = {"revenue": SHOP_REVENUE, **({"other": every_m1_rows} if extra == "read" else {})}
        response = rt.query({"select": _select(**items)})
        (row,) = typed_rows(response)
        assert row["revenue"] == 74
        if extra == "read":  # store b's orders 2, 5, 9 and 10
            assert row["other"] == 400
        sql = response["rendered_sql"]
        assert f"orders.{RENAMED_AMOUNT}" in sql
        assert ("AS m1_rows_2" in sql) is (extra == "read")
    finally:
        rt.close()


@pytest.mark.parametrize("else_value", [None, 0, 2])
def test_case_sum_preserves_explicit_else_contributions(
    shop_package: Path, else_value: int | None
) -> None:
    config = load_package_config(str(shop_package))
    payload = _amount_when(IN_STORE_A, otherwise=else_value)
    config = _with_measure(config, payload, aggregate_relations=[])
    literal = "NULL" if else_value is None else str(else_value)
    # ELSE NULL has no contributing row where the condition fails, so its empty sum
    # settles to 0. Explicit non-NULL ELSE values remain in the ordinary SQL sum.
    value = f"SUM(CASE WHEN store_id = 'a' THEN amount ELSE {literal} END)"
    if else_value is None:
        value = f"CASE WHEN COUNT(CASE WHEN store_id = 'a' THEN 1 END) = 0 THEN 0 ELSE {value} END"
    rt = Runtime.from_config(config, source_path=str(shop_package))
    try:
        # Store a's May is one order with no amount; May's other order, 8, has no store and
        # reads the ELSE value.
        for by_store, key, expected in [
            (False, (None, "2024-05"), else_value),
            (True, ("a", "2024-05"), None),
        ]:
            response = rt.query(
                {
                    "select": _select(revenue=SHOP_REVENUE),
                    "group_by": [SHOP_STORE] if by_store else [],
                    "time": SHOP_MONTH,
                }
            )
            got = {
                (row.get(SHOP_STORE), str(row["period"])[:7]): row["revenue"]
                for row in typed_rows(response)
            }
            gold = _gold(
                rt,
                f"SELECT {'store_id' if by_store else 'NULL'} AS s, "
                f"date_trunc('month', ordered_at) AS month, {value} AS revenue "
                f"FROM orders GROUP BY {'1, 2' if by_store else '2'}",
            )
            assert got == {(row["s"], str(row["month"])[:7]): row["revenue"] for row in gold}
            assert got[key] == expected
    finally:
        rt.close()


@pytest.mark.parametrize("else_null", [False, True])
def test_a_case_with_two_branches_is_zero_only_where_no_row_meets_either(
    shop_package: Path, else_null: bool
) -> None:
    """A second branch is still a condition: a group none of whose rows meets a branch reads 0,
    as with one branch, and a group whose matching rows have no amount stays unknown."""
    payload = _amount_when(IN_STORE_A, IN_STORE_B, otherwise=None if else_null else "none")
    config = _with_measure(load_package_config(str(shop_package)), payload, aggregate_relations=[])
    rt = Runtime.from_config(config, source_path=str(shop_package))
    try:
        response = rt.query(
            {"select": _select(revenue=SHOP_REVENUE), "group_by": [SHOP_STORE], "time": SHOP_MONTH}
        )
        got = {
            (row[SHOP_STORE], str(row["period"])[:7]): row["revenue"]
            for row in typed_rows(response)
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
    response = shop.query({"select": _select(a=_amount_if(IN_STORE_A)), "group_by": [SHOP_ORDER]})
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


CASE_UNDER_ARITHMETIC = {
    "kind": "arithmetic",
    "op": "divide",
    "left": _amount_when(IN_STORE_A),
    "right": {"kind": "literal", "value": 100.0},
}


def test_a_case_under_arithmetic_keeps_the_base_settlement(shop_package: Path) -> None:
    """The fallback makes a no-match group zero when amounts are observed elsewhere; a scope
    containing only the matched unknown amount remains NULL, as on the base path."""
    base = load_package_config(str(shop_package))
    raw = next(row for row in base.measures if row.id == SHOP_REVENUE["measure"])
    config = _with_measure(base, CASE_UNDER_ARITHMETIC, aggregate_relations=[])
    config = replace(
        config, measures=[*config.measures, replace(raw, id="measure.shop.raw_revenue")]
    )
    rt = Runtime.from_config(config, source_path=str(shop_package))
    try:
        for unknown_only in (False, True):
            query = {
                "select": _select(
                    revenue=SHOP_REVENUE,
                    raw={**SHOP_REVENUE, "measure": "measure.shop.raw_revenue"},
                ),
                "group_by": [SHOP_STORE],
                "time": SHOP_MONTH,
            }
            if unknown_only:
                # A nested CASE can't tell unknown amounts from no rows, so the dataset scope
                # refuses its filtered query (test_observation_scope); the query scope is the
                # base's.
                query["where"] = [{"field": SHOP_ORDER, "op": "is", "value": 7}]
                query["observation_scope"] = "query"
            response = rt.query(query)
            rows = {(row[SHOP_STORE], str(row["period"])[:7]): row for row in typed_rows(response)}
            gold = _gold(
                rt,
                "WITH amounts AS (SELECT store_id AS s, date_trunc('month', ordered_at) AS month, "
                "SUM(CASE WHEN store_id = 'a' THEN amount END / 100.0) AS revenue FROM orders "
                + ("WHERE order_id = 7 " if unknown_only else "")
                + "GROUP BY 1, 2) SELECT s, month, CASE WHEN COUNT(revenue) OVER () > 0 "
                "THEN COALESCE(revenue, 0) END AS revenue FROM amounts",
            )
            got = {key: row["revenue"] for key, row in rows.items()}
            assert got == {(row["s"], str(row["month"])[:7]): row["revenue"] for row in gold}
            if unknown_only:
                assert got == {("a", "2024-05"): None}
            else:
                assert got[("b", "2023-11")] == 0
            # The nested CASE fallback must not turn an ordinary sum's unknown amount into zero.
            assert rows[("a", "2024-05")]["raw"] is None
            assert_settled_in_one_place(compile_query(config, Registry(config), query), config)
    finally:
        rt.close()


@pytest.mark.parametrize("join_column", ["__source_rows", "m1_rows", "__source_value"])
def test_a_source_rollup_count_never_shadows_a_physical_join_column(
    tmp_path: Path, join_column: str
) -> None:
    """Refunds reach the customer channel through orders; intermediate counts and values
    must not read a physical join column as an aggregate."""
    composite = join_column != "__source_rows"
    value_collision = join_column == "__source_value"
    statements = (
        [
            f"ALTER TABLE refunds ADD COLUMN {join_column} INTEGER",
            f"UPDATE refunds SET {join_column} = 1",
            "ALTER TABLE orders ADD COLUMN pair_key INTEGER",
            "UPDATE orders SET pair_key = 1",
        ]
        if composite
        else ["ALTER TABLE refunds RENAME COLUMN order_id TO __source_rows"]
    )
    package = _shop_package(tmp_path, *statements)
    config = load_package_config(str(package))
    if not value_collision:
        goods = {"kind": "column", "column": "goods_amount"}
        case = {"kind": "case", "whens": [{"when": _refund_type_is("goods"), "then": goods}]}
        config = _with_measure(config, case, SHOP_GOODS["measure"])
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
                source_columns=["order_id", join_column] if composite else ["__source_rows"],
                target_columns=["order_id", "pair_key"] if composite else row.target_columns,
            )
            if row.source_entity == "entity.shop_refund" and row.source_column == "order_id"
            else row
            for row in config.relationships
        ],
    )
    rt = Runtime.from_config(config, source_path=str(package))
    try:
        channel = "dimension.shop_customer_channel"
        query = {"select": _select(goods=SHOP_GOODS), "group_by": [channel]}
        response = rt.query(query)
        got = {(row[channel], row["goods"]) for row in typed_rows(response)}
        join = (
            f"r.order_id = o.order_id AND r.{join_column} = o.pair_key"
            if composite
            else "r.__source_rows = o.order_id"
        )
        reference_value = (
            "SUM(r.goods_amount)"
            if value_collision
            else "CASE WHEN COUNT(CASE WHEN r.refund_type = 'goods' THEN 1 END) = 0 "
            "THEN 0 ELSE SUM(CASE WHEN r.refund_type = 'goods' THEN r.goods_amount END) END"
        )
        gold = _gold(
            rt,
            f"SELECT s.channel, {reference_value} AS goods "
            f"FROM refunds r LEFT JOIN orders o ON {join} "
            "LEFT JOIN signups s ON o.customer_id = s.customer_id GROUP BY 1",
        )
        assert got == {(row["channel"], row["goods"]) for row in gold}
        if value_collision:
            assert got == {("store", 10), ("web", None), (None, 2)}
        else:
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
            if join_column == "m1_rows":
                assert "m1_rows_2" in names
            elif value_collision:
                assert "__source_value_2" in names
        assert_settled_in_one_place(compiled, config)
    finally:
        rt.close()


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
    shop: Runtime, kind: str, width: int, expected: dict[str, tuple[int | None, int | None]]
) -> None:
    """Revenue plus, and minus, goods refunds: each operand is windowed on its own."""
    expressions = {
        op: {
            "kind": kind,
            "input": {"kind": "arithmetic", "op": op, "left": SHOP_REVENUE, "right": SHOP_GOODS},
        }
        for op in ("add", "subtract")
    }
    for expression in expressions.values():
        if kind == "rolling":
            expression["window"] = {"unit": "month", "value": width}
        elif kind == "period_to_date":
            expression["period"] = "year"
    response = shop.query({"select": _select(**expressions), "time": {**SHOP_MONTH, "fill": True}})
    got = {
        row["period"].strftime("%Y-%m"): (row["add"], row["subtract"])
        for row in typed_rows(response)
    }
    assert {month: got[month] for month in expected} == expected


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
        NET_HUNDREDTHS: {
            "kind": "arithmetic",
            "op": "divide",
            "left": {"metric": NET},
            "right": HUNDRED,
        },
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
        yield opened(rt)
    finally:
        rt.close()


def _summing_window_query(kind: str, input_expr: dict[str, Any]) -> dict[str, Any]:
    expression = {"kind": kind, "input": input_expr}
    if kind == "rolling":
        expression["window"] = {"unit": "month", "value": 3}
    elif kind == "period_to_date":
        expression["period"] = "year"
    return {"select": _select(value=expression), "time": {**SHOP_MONTH, "fill": True}}


@pytest.mark.parametrize(
    ("kind", "input_expr"),
    [
        ("rolling", {"kind": "arithmetic", "op": "multiply", "left": NET, "right": TWO}),
        ("cumulative", {"kind": "arithmetic", "op": "divide", "left": NET, "right": HUNDRED}),
        ("period_to_date", {"kind": "arithmetic", "op": "add", "left": NET, "right": SHOP_REVENUE}),
        ("rolling", {"kind": "ratio", "numerator": NET, "denominator": SHOP_REVENUE}),
        ("cumulative", NET_HUNDREDTHS),
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
    for code, details in [
        (issue["code"], issue["details"]),
        (raised.value.code, raised.value.details),
    ]:
        assert code == "ROLLUP_UNSAFE"
        assert details["unsupported_construct"] == "nested_metric_window_input"
        assert details["construct"] == kind
        assert details["input"] == NET


@pytest.mark.parametrize(
    ("input_expr", "expected"),
    [
        ({"kind": "arithmetic", "op": "multiply", "left": NET_INLINE, "right": TWO}, 72),
        (NET, 36),
    ],
    ids=["inline_times_two", "whole_input_metric"],
)
def test_a_summing_window_answers_inline_operands_and_a_whole_input_metric(
    shop_with_net: Runtime, input_expr: dict[str, Any], expected: int
) -> None:
    response = shop_with_net.query(_summing_window_query("rolling", input_expr))
    got = {row["period"].strftime("%Y-%m"): row["value"] for row in typed_rows(response)}
    # November 2023 to January 2024, each part summed over the base rows on its own: revenue
    # 10 + 5 + 7 + 20 less order 2's goods refunds, 5 + 1, is 36.
    assert got["2024-01"] == expected


def test_a_sum_whose_rows_all_lack_a_value_is_unknown_and_its_count_is_not(shop: Runtime) -> None:
    # A named window answers every period of its grain.
    window = {"start": "2024-05-01", "end": "2024-07-01"}
    response = shop.query(
        {
            "select": _select(revenue=SHOP_REVENUE, orders=SHOP_ORDERS),
            "group_by": [SHOP_STORE],
            "time": {**SHOP_MONTH, **window},
        }
    )
    got = {
        (row[SHOP_STORE], str(row["period"])[:7]): (row["revenue"], row["orders"])
        for row in typed_rows(response)
    }
    expected = {("a", "2024-05"): (None, 1), (None, "2024-05"): (6, 1), ("b", "2024-06"): (3, 1)}
    # The groups with no rows, where both measures have data in scope.
    expected |= {key: (0, 0) for key in [("a", "2024-06"), ("b", "2024-05"), (None, "2024-06")]}
    assert got == expected
    assert "FROM orders_monthly" not in response["rendered_sql"]


def test_an_unknown_value_meets_no_threshold_not_even_one_zero_passes(shop: Runtime) -> None:
    """A threshold 0 passes keeps every order that doesn't fail it, the ones with no rows too;
    order 7's unknown revenue is not one of them: orders 6, 9 and 11 are below 5."""
    below_5 = {
        "entity": "entity.shop_order",
        "having": [{"field": SHOP_REVENUE["measure"], "op": "lt", "value": 5}],
        "over": "all_time",
    }
    response = shop.query(
        {"select": _select(orders=SHOP_ORDERS), "group_by": [SHOP_STORE], "where": [below_5]}
    )
    assert {row[SHOP_STORE]: row["orders"] for row in typed_rows(response)} == {"a": 2, "b": 1}


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
    # An entity filter names one measure or metric; the compiler's own form takes an inline
    # combination of several.
    response = shop.query_internal(
        {
            "select": _select(orders=SHOP_ORDERS),
            "group_by": [SHOP_ORDER],
            "where": [{"field": SHOP_ORDER, "op": "not", "value": 7}],
            "metric_filters": [{"expression": no_refund, "op": "=", "value": True}],
            # A where filter beside a metric predicate is judged inside the filters only.
            "observation_scope": "query",
        }
    )
    # Every order but 7 and the refunded 2, 4 and 6.
    assert sorted(row[SHOP_ORDER] for row in typed_rows(response)) == [1, 3, 5, 8, 9, 10, 11]


@pytest.mark.parametrize(
    ("payload", "beside_a_distribution", "routed"),
    [
        (_amount_when(IN_STORE_A), False, False),
        (_amount_when(IN_STORE_A, IN_STORE_B, otherwise=None), False, False),
        (_amount_when(IN_STORE_A, otherwise=0), False, True),
        (_amount_when(IN_STORE_A, IN_STORE_B, otherwise=0), True, True),
        (_amount_when(IN_STORE_A, IN_STORE_B), True, True),
        (_amount_when(IN_STORE_A, otherwise=None), True, True),
        # A CASE under arithmetic is never routed, alone or beside a distribution.
        (CASE_UNDER_ARITHMETIC, False, False),
        (CASE_UNDER_ARITHMETIC, True, False),
    ],
)
def test_a_rollup_never_answers_a_conditional_sum(
    shop_package: Path, payload: dict[str, Any], beside_a_distribution: bool, routed: bool
) -> None:
    """A rollup's sum can't tell rows that all fail a CASE condition (0) from rows that meet it
    with no value (NULL), so routing leaves such a measure on the base table. A non-NULL ELSE
    reads every row, as the base path's row count does, so the rollup may answer it. Beside a
    distribution the plan keeps the earlier settlement, where the rollup answers any of them."""
    config = _with_measure(load_package_config(str(shop_package)), payload)
    query = {"select": _select(revenue=SHOP_REVENUE), "group_by": [SHOP_STORE], "time": SHOP_MONTH}
    if beside_a_distribution:
        query["select"] += REFUNDS_BESIDE_A_MEDIAN["select"][1:]
    compiled = compile_query(config, Registry(config), {"version": 1, **query})
    (leaf,) = compiled["logical_plan"].measure_plans
    assert (leaf.aggregate_relation_id != "") is routed
    if not routed:
        assert set(leaf.aggregate_relation_rejections.values()) == {"aggregation_not_reaggregable"}


# -- a path that skips the guard is refused -------------------------------------------------
# Each case forces one bypass; the guard refuses rather than read 0 or NULL by accident.


def _unguarded_source(config: Any, payload: dict[str, Any]) -> Any:
    return compiler._compile_query_sql_ast(config, payload, project_cut=True, guard_empty=False)


def _row_counts_named_revenue(plan: Any, taken: Any) -> dict[str, str]:
    return {row.bound_measure.alias: "Revenue" for row in plan.measure_plans}


NO_ROW_MARKERS = [(module, "_row_markers", lambda *args: []) for module in (leaves, row_counts)]
SHOP_REVENUE_BY_STORE = {
    "select": _select(revenue=SHOP_REVENUE),
    "group_by": [SHOP_STORE],
    "time": SHOP_MONTH,
}
UNOBSERVED = {"outputs": ["revenue", "orders", "items"], "observation_scope": "dataset"}
BYPASSES = {
    # Lowering probes no measure's own rows, or never asks for the scope, while the guard reads
    # it: the filtered query is refused, never judged inside its filters.
    "scope_unprobed": (
        "jaffle",
        SHAPES["filtered_by_store"],
        [(sql_lowering, "_dataset_scope", lambda *args: None)],
        UNOBSERVED,
    ),
    "scope_unasked": (
        "jaffle",
        SHAPES["filtered_by_store"],
        [(sql_lowering, "observes_dataset", lambda *args: False)],
        UNOBSERVED,
    ),
    # Lowering builds no guard, and the check that works it out again refuses.
    "projection_unguarded": (
        "jaffle",
        SHAPES["single_measure"],
        [(sql_lowering, "zero_aliases", lambda *args: {})],
        {},
    ),
    "distribution_unguarded": (
        "jaffle",
        SHAPES["beside_a_distribution"],
        [(distribution, "zero_outputs", lambda *args: {})],
        {},
    ),
    # No leaf counts its rows, so the guard can't tell a group with no rows from one whose
    # values are all unknown.
    **{
        f"no_row_count_{shape}": ("jaffle", SHAPES[shape], NO_ROW_MARKERS, {"missing": "row_count"})
        for shape in (
            "single_measure",
            "same_source_multi_measure",
            "filled_series",
            "window_total",
            "sum_of_two_measures",
        )
    },
    # An entity the source doesn't list reads like the ones it does only while the source is
    # settled as a whole, so a source compiled without the guard is refused, not gated.
    **{
        f"unguarded_source_{shape}": (
            "jaffle",
            SHAPES[shape],
            [(compiler, "_compile_predicate_source_ast", _unguarded_source)],
            {},
        )
        for shape in ("predicate_case_count", "predicate_zero_passes")
    },
    "row_count_named_like_an_output": (
        "jaffle",
        SHAPES["single_measure"],
        [
            (module, "_row_count_names", _row_counts_named_revenue)
            for module in (row_counts, sql_lowering)
        ],
        {"row_counts_named_like": ["revenue"]},
    ),
    "time_leaf_unscoped": (
        "jaffle",
        {
            "select": _select(revenue=REVENUE),
            "time": {
                "temporal_role": ORDER_TIME,
                "grain": "month",
                "start": "2017-04-01",
                "end": "2017-05-01",
            },
        },
        [(lowering_common, "record_leaf_scope", lambda *args: None)],
        {},
    ),
    "window_total_unscoped": (
        "shop",
        {
            "select": _select(b=STORE_B_ORDERS),
            "time": {"temporal_role": SHOP_ROLE, "start": "2023-12-01", "end": "2023-12-08"},
        },
        [(leaves, "_record_time_scope", lambda *args, **kwargs: None)],
        {},
    ),
    # The routed path's leaf counts no rows.
    "rollup_leaf_without_row_count": (
        "shop",
        SHOP_REVENUE_BY_STORE,
        [
            (module, "reads_every_row", lambda measure: False)
            for module in (leaves, lowering_common)
        ],
        {"missing": "row_count"},
    ),
    # A nested CASE forced onto the rollup, whose sum can't tell unknown amounts from no rows.
    "nested_case_on_a_rollup": (
        "shop_case_under_arithmetic",
        SHOP_REVENUE_BY_STORE,
        [
            (
                planning,
                "_select_aggregate_relation",
                lambda *args, **kwargs: ("aggregate_relation.orders_monthly", {}),
            )
        ],
        {"measures": [SHOP_REVENUE["measure"]]},
    ),
}


@pytest.mark.parametrize("case", BYPASSES)
def test_a_path_that_skips_the_guard_is_refused(
    config: Any, shop_package: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    package, query, patches, details = BYPASSES[case]
    if package != "jaffle":
        config = load_package_config(str(shop_package))
    if package == "shop_case_under_arithmetic":
        config = _with_measure(config, CASE_UNDER_ARITHMETIC)
    query = {"version": 1, **query}
    if details.get("observation_scope"):
        # The refusal's fix: judged inside the filters, the same query compiles.
        compile_query(config, Registry(config), {**query, "observation_scope": "query"})
    for module, name, replacement in patches:
        monkeypatch.setattr(module, name, replacement)
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), query)
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert details.items() <= raised.value.details.items()
