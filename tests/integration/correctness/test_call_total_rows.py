"""Subtotal and grand-total rows (a call's ``totals``), checked against reference SQL.

The ledger freezes one (``shop/store-and-channel-revenue-with-store-subtotals-and-a-grand-total``);
this file checks each level against its own reference, the period, the order of the rows,
having and limit, a link, refusals and the policies that refuse total rows.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

STORE = "dimension.shop_order_store_id"
CHANNEL = "dimension.shop_customer_channel"
ORDERED_AT = "temporal_role.shop_order_ordered_at"
REVENUE = "measure.shop.revenue"
ORDERS = "measure.shop.order_count"
CUSTOMERS = "measure.shop.buying_customers"


def _levels(rows: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    out: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["total_over"], []).append(row)
    return out


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_each_level_is_the_call_at_its_grain(request, backend):
    """Store subtotals across channels, then the grand total: each counts its customers once
    (customer 102 buys at both stores; customer 101 at store a and with no store)."""
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    rows = runtime.query(
        {"select": [REVENUE, CUSTOMERS], "group_by": [STORE, CHANNEL], "totals": [STORE]}
    )["rows"]
    levels = _levels(rows)
    assert list(levels) == [None, CHANNEL, f"{STORE}, {CHANNEL}"]
    by_store = {
        store: (Decimal(revenue), customers)
        for store, revenue, customers in source.reference(
            "SELECT store_id, SUM(amount), COUNT(DISTINCT customer_id) FROM orders GROUP BY 1"
        )
    }
    assert {
        row[STORE]: (Decimal(row["revenue"]), row["buying_customers"]) for row in levels[CHANNEL]
    } == by_store
    assert all(row[CHANNEL] is None for row in levels[CHANNEL])
    [[revenue, customers]] = source.reference(
        "SELECT SUM(amount), COUNT(DISTINCT customer_id) FROM orders"
    )
    [grand] = levels[f"{STORE}, {CHANNEL}"]
    assert (grand[STORE], grand[CHANNEL]) == (None, None)
    assert (Decimal(grand["revenue"]), grand["buying_customers"]) == (revenue, customers)
    assert grand["buying_customers"] < sum(row["buying_customers"] for row in levels[CHANNEL])


def test_total_rows_keep_the_period(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    rows = runtime.query(
        {
            "select": [CUSTOMERS],
            "group_by": [STORE],
            "time": {"temporal_role": ORDERED_AT, "grain": "year"},
            "totals": [],
        }
    )["rows"]
    grand = {row["period"]: row["buying_customers"] for row in _levels(rows)[STORE]}
    reference = duckdb_backend.reference(
        "SELECT CAST(date_trunc('year', ordered_at) AS DATE), COUNT(DISTINCT customer_id) "
        "FROM orders GROUP BY 1"
    )
    assert grand == {period.isoformat(): customers for period, customers in reference}


def test_total_rows_follow_the_calls_rows_which_having_and_limit_cut(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    result = runtime.query(
        {
            "select": [REVENUE],
            "group_by": [STORE],
            "having": [{"field": "revenue", "op": "gt", "value": 10}],
            "order_by": [{"field": "revenue", "direction": "desc"}],
            "limit": 1,
            "totals": [],
        }
    )
    [[everything]] = duckdb_backend.reference("SELECT SUM(amount) FROM orders")
    assert [(row[STORE], Decimal(row["revenue"]), row["total_over"]) for row in result["rows"]] == [
        ("a", Decimal(43), None),
        (None, everything, STORE),
    ]
    assert result["assumptions"] == [
        {
            "code": "TOTALS",
            "levels": [STORE],
            "message": "Rows whose total_over names columns add those columns up: each is the "
            "call at that grain, computed from the rows the call keeps before having and limit, "
            "never added up from the returned rows.",
        }
    ]
    assert result["output_columns"][-1]["field"] == "total_over"


def test_a_grand_total_across_a_link_counts_each_person_once(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    headcount, department = "measure.shop.headcount", "dimension.shop_room_rows_department"
    rows = runtime.query({"select": [headcount], "group_by": [department], "totals": []})["rows"]
    [everyone] = runtime.query({"select": [headcount]})["rows"]
    [grand] = _levels(rows)[department]
    assert grand["headcount"] == everyone["headcount"]
    assert sum(row["headcount"] for row in _levels(rows)[None]) > everyone["headcount"]


@pytest.mark.parametrize(
    ("call", "path"),
    [
        ({"select": [ORDERS], "group_by": [STORE], "totals": [CHANNEL]}, "totals[0]"),
        ({"select": [ORDERS], "group_by": [STORE], "totals": [STORE, STORE]}, "totals[1]"),
        ({"select": [ORDERS], "totals": []}, "totals"),
        ({"select": [ORDERS], "group_by": [STORE], "totals": STORE}, "totals"),
        (
            {
                "select": [{"expression": ORDERS, "as": "total_over"}],
                "group_by": [STORE],
                "totals": [],
            },
            "totals",
        ),
        (
            {
                "select": [{"expression": {"kind": "share", "input": ORDERS, "over": [STORE]}}],
                "group_by": [STORE],
                "totals": [],
            },
            "totals",
        ),
        (
            {
                "select": [{"expression": {"kind": "total", "input": ORDERS, "over": [STORE]}}],
                "group_by": [STORE, CHANNEL],
                "totals": [],
            },
            "totals",
        ),
    ],
    ids=["not-grouped", "twice", "no-group-by", "not-a-list", "named-total-over", "share", "total"],
)
def test_totals_are_refused_at_their_path(duckdb_backend, call, path):
    [error] = duckdb_backend.runtimes["utc_authored"].validate(call)["errors"]
    assert error["code"] in {"INVALID_QUERY", "DUPLICATE_OUTPUT_ALIAS"}
    assert error["details"]["path"] == path


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


def test_a_required_grouping_refuses_total_rows(per_store_revenue):
    """The grand-total row reads revenue grouped by nothing."""
    call = {"select": [REVENUE], "group_by": [STORE]}
    assert per_store_revenue.query(call)["rows"]
    with pytest.raises(SemanticLayerError) as raised:
        per_store_revenue.query({**call, "totals": []})
    assert raised.value.code == "POLICY_DENIED"
    assert "the total rows add it up across them" in str(raised.value)
