"""A total: its input at a coarser grain beside each row, checked against reference SQL.

The ledger freezes two (``shop/store-customers-over-every-month-count-each-customer-once``
and ``shop/quarter-revenue-across-stores-beside-each-store``); this file checks each measure
kind at its grain, the rows the call keeps, windows and filled series, a coarser group with
no rows of the input, links, refusals and the policy that requires a grouping.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

STORE = "dimension.shop_order_store_id"
ORDERED_AT = "temporal_role.shop_order_ordered_at"
REVENUE = "measure.shop.revenue"
ORDERS = "measure.shop.order_count"
CUSTOMERS = "measure.shop.buying_customers"
NOW = "2024-08-15T00:00:00Z"


def _total(input_id: str, over: list[str], alias: str = "total") -> dict[str, Any]:
    return {"expression": {"kind": "total", "input": input_id, "over": over}, "as": alias}


def _by_key(rows: list[dict[str, Any]], key: str, column: str = "total") -> dict[Any, Any]:
    """Each remaining key's total, which every one of its rows must read alike."""
    out: dict[Any, Any] = {}
    for row in rows:
        value = row[column]
        assert out.setdefault(row[key], value) == value, (row[key], out[row[key]], value)
    return out


def _period(value: Any) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)


QUARTER = {"temporal_role": ORDERED_AT, "grain": "quarter"}

# (input, over, the key the total keeps, reference SQL: key, value)
AT_ITS_GRAIN = {
    # Every amount of the store, not the average of its quarters' averages.
    "average-over-period": (
        "measure.shop.average_order",
        ["period"],
        STORE,
        "SELECT store_id, AVG(amount) FROM orders GROUP BY store_id",
    ),
    # The quarter's revenue over its orders, not the average of the stores' ratios.
    "ratio-over-store": (
        "metric.shop.average_order_value",
        [STORE],
        "period",
        "SELECT date_trunc('quarter', ordered_at), "
        "SUM(amount) / CAST(COUNT(order_id) AS DOUBLE PRECISION) "
        "FROM orders GROUP BY 1",
    ),
    # Customer 102 buys at both stores in the first quarter of 2024: counted once.
    "distinct-count-over-store": (
        CUSTOMERS,
        [STORE],
        "period",
        "SELECT date_trunc('quarter', ordered_at), COUNT(DISTINCT customer_id) "
        "FROM orders GROUP BY 1",
    ),
}


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
@pytest.mark.parametrize("case", AT_ITS_GRAIN)
def test_a_total_is_its_input_computed_at_the_coarser_grain(request, backend, case):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    input_id, over, key, reference = AT_ITS_GRAIN[case]
    result = runtime.query(
        {"select": [input_id, _total(input_id, over)], "group_by": [STORE], "time": QUARTER}
    )
    expected = {(_period(k) if key == "period" else k): v for k, v in source.reference(reference)}
    assert _by_key(result["rows"], key) == pytest.approx({k: float(v) for k, v in expected.items()})


def test_a_total_states_what_it_reads_in_the_inputs_unit(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    result = runtime.query({"select": [REVENUE, _total(REVENUE, [STORE])], "group_by": [STORE]})
    assert result["assumptions"] == [
        {
            "code": "TOTAL_OVER",
            "column": "total",
            "input": REVENUE,
            "over": [STORE],
            "message": f"total is {REVENUE} across {STORE} in the rows the call keeps, computed "
            "at that grain, not added up from the rows.",
        }
    ]
    [column] = [row for row in result["output_columns"] if row["field"] == "total"]
    assert (column["semantic_id"], column["type"]) == ("", "currency")
    # Over every column: the grand total on every row, the unknown store's row included.
    [[everything]] = duckdb_backend.reference("SELECT SUM(amount) FROM orders")
    assert {Decimal(row["total"]) for row in result["rows"]} == {everything}
    assert len(result["rows"]) == 3


def test_a_total_reads_the_rows_the_call_keeps(duckdb_backend):
    """The call's where and entity filters cut the total's rows; having and limit act after."""
    runtime = duckdb_backend.runtimes["utc_authored"]
    repeat_buyers = {
        "entity": "entity.shop_customer",
        "having": [{"field": ORDERS, "op": "gte", "value": 2}],
        "over": "all_time",
    }
    call = {
        "select": [ORDERS, _total(REVENUE, ["period"])],
        "group_by": [STORE],
        "time": QUARTER,
        "where": [{"field": STORE, "op": "not", "value": "b"}, repeat_buyers],
        # Beside an entity filter, empty groups are judged inside the filters.
        "observation_scope": "query",
    }
    [*reference] = duckdb_backend.reference(
        "SELECT store_id, SUM(amount) FROM orders WHERE store_id IS DISTINCT FROM 'b' "
        "AND customer_id IN (SELECT customer_id FROM orders GROUP BY 1 HAVING COUNT(*) >= 2) "
        "GROUP BY 1"
    )
    rows = runtime.query(call)["rows"]
    assert _by_key(rows, STORE) == {store: str(int(total)) for store, total in reference}
    kept = runtime.query(
        {
            **call,
            "having": [{"field": "order_count", "op": "gte", "value": 2}],
            "order_by": [{"field": "order_count", "direction": "desc"}],
            "limit": 1,
        }
    )["rows"]
    # Store a keeps two quarters of two orders; its total still reads all four of its orders.
    assert [(row[STORE], row["order_count"], row["total"]) for row in kept] == [("a", 2, "41")]
    # A having may read the total's own column.
    over_ten = runtime.query({**call, "having": [{"field": "total", "op": "gt", "value": 10}]})
    assert {row[STORE] for row in over_ten["rows"]} == {"a"}


def test_a_total_over_a_window_and_a_filled_series(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    window = {"temporal_role": ORDERED_AT, "start": "2024-01-01", "end": "2024-07-01"}
    result = runtime.query(
        {"select": [ORDERS, _total(ORDERS, [STORE])], "group_by": [STORE], "time": window}
    )
    [[orders]] = duckdb_backend.reference(
        "SELECT COUNT(*) FROM orders WHERE ordered_at >= '2024-01-01' AND ordered_at < '2024-07-01'"
    )
    assert {row["total"] for row in result["rows"]} == {orders}
    # A named window answers each of its months; April has no order anywhere: 0, not NULL.
    filled = runtime.query(
        {
            "select": [ORDERS, _total(ORDERS, [STORE])],
            "group_by": [STORE],
            "time": {
                "temporal_role": ORDERED_AT,
                "grain": "month",
                "last": {"unit": "month", "value": 4},
            },
        },
        now=NOW,
    )
    assert _by_key(filled["rows"], "period") == {
        "2024-04-01": 0,
        "2024-05-01": 2,
        "2024-06-01": 1,
        "2024-07-01": 1,
    }


def test_a_coarser_group_with_no_rows_of_the_input_reads_what_its_rows_read(duckdb_backend):
    """The unknown store's one order has no refund: its total reads the 0 its month reads, as
    the call at the store grain does."""
    runtime = duckdb_backend.runtimes["utc_authored"]
    refunds = "measure.shop.refund_count"
    monthly = runtime.query(
        {
            "select": [ORDERS, refunds, _total(refunds, ["period"])],
            "group_by": [STORE],
            "time": {"temporal_role": ORDERED_AT, "grain": "month"},
        }
    )
    by_store = runtime.query({"select": [ORDERS, refunds], "group_by": [STORE]})
    assert _by_key(monthly["rows"], STORE) == {
        row[STORE]: row["refund_count"] for row in by_store["rows"]
    }
    assert _by_key(monthly["rows"], STORE)[None] == 0


def test_a_total_across_a_link_counts_each_person_once(duckdb_backend):
    """Grouped by department, a person in two departments counts in both; the total is the
    call without the department, so each person counts once."""
    runtime = duckdb_backend.runtimes["utc_authored"]
    headcount, department = "measure.shop.headcount", "dimension.shop_room_rows_department"
    rows = runtime.query(
        {"select": [headcount, _total(headcount, [department])], "group_by": [department]}
    )["rows"]
    [everyone] = runtime.query({"select": [headcount]})["rows"]
    assert {row["total"] for row in rows} == {everyone["headcount"]}
    assert sum(row["headcount"] for row in rows) > everyone["headcount"]


@pytest.mark.parametrize(
    ("call", "code", "path"),
    [
        (  # a total is a select item of its own
            {
                "select": [
                    {
                        "expression": {
                            "kind": "arithmetic",
                            "op": "divide",
                            "left": ORDERS,
                            "right": {"kind": "total", "input": ORDERS, "over": [STORE]},
                        },
                        "as": "part",
                    }
                ],
                "group_by": [STORE],
            },
            "INVALID_QUERY",
            "select[0].expression.right",
        ),
        (
            {"select": [_total(ORDERS, ["dimension.shop_customer_channel"])], "group_by": [STORE]},
            "INVALID_QUERY",
            "select[0].expression.over[0]",
        ),
        ({"select": [_total(ORDERS, ["period"])], "group_by": [STORE]}, "INVALID_QUERY", None),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "total",
                            "input": {"kind": "measure", "measure": ORDERS},
                            "over": [STORE],
                        }
                    }
                ],
                "group_by": [STORE],
            },
            "INVALID_QUERY",
            "select[0].expression.input",
        ),
        (  # a distribution has no coarser call yet
            {"select": [_total("metric.shop.order_revenue_median", [STORE])], "group_by": [STORE]},
            "REWRITE_NOT_SUPPORTED",
            None,
        ),
    ],
    ids=[
        "nested",
        "over-another-column",
        "period-without-grain",
        "input-not-an-id",
        "distribution",
    ],
)
def test_a_total_is_refused_at_its_path(duckdb_backend, call, code, path):
    runtime = duckdb_backend.runtimes["utc_authored"]
    [error] = runtime.validate(call)["errors"]
    assert error["code"] == code
    if path is not None:
        assert error["details"]["path"] == path


def test_a_package_metric_cannot_be_a_total():
    total = {"kind": "total", "input": {"measure": ORDERS}, "over": [STORE]}
    with pytest.raises(SemanticLayerError, match="a package metric can't be one"):
        parse_semantic_expression(total, context="config")


@pytest.fixture
def per_store_revenue(duckdb_backend):
    """Revenue may only be read grouped by store."""
    base = duckdb_backend.runtimes["utc_authored"]
    policy = SemanticPolicyConfig(
        id="policy.shop.revenue_per_store",
        kind="metric_constraint",
        object_ids=[REVENUE],
        config={"required_group_by": [STORE]},
    )
    config = base.package_config
    runtime = Runtime.from_config(
        replace(config, semantic_policies=[*config.semantic_policies, policy]),
        source_path=base.source_path,
    )
    try:
        yield runtime
    finally:
        runtime.close()


def test_a_total_over_a_required_grouping_is_refused(per_store_revenue):
    """The total's coarser call reads revenue across stores, which the policy forbids."""
    call = {"select": [REVENUE, _total(REVENUE, [STORE])], "group_by": [STORE], "time": QUARTER}
    with pytest.raises(SemanticLayerError) as raised:
        per_store_revenue.query(call)
    assert raised.value.code == "POLICY_DENIED"
    assert "the total in 'total' reads it across them" in str(raised.value)
    # Over the period, each store's total is still grouped by store.
    allowed = per_store_revenue.query({**call, "select": [REVENUE, _total(REVENUE, ["period"])]})
    assert allowed["rows"]
