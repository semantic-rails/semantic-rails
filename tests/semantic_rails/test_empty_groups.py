"""Empty groups: NULL when there is no data, 0 when there is data of nothing.

Gold values come from raw SQL on the seeded tables. The differential corpus in
``tests/integration/correctness`` holds the same rule to independent SQL on DuckDB and Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails import compiler
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.compiler_parts.empty_groups import resolves_to_zero
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime, _no_data_in_scope_warnings
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.empty_groups_invariant import assert_settled_in_one_place
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
