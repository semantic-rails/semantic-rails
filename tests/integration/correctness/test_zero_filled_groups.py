"""ZERO_FILLED_GROUPS: the groups whose count or sum reads 0 because none of its own rows reach
them, while another measure's rows hold the group, checked against reference SQL.

Customers are parents with some children missing: 107 signed up and never ordered, and 108
ordered without signing up. Grouped by customer beside each other, signups and orders join on
the customer, so 107 reads 0 orders and 108 reads 0 signups (data of nothing, not missing data).
The statement marks those groups itself, in a hidden column per measure (1 or 0); the runtime
strips the columns and the note counts the marked rows that read 0, with no other query. Every
warehouse compiles the marks; a series, one leaf and an ungrouped call can't hold such a group,
so their statements carry none.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.db_parts.base import QueryRows
from semantic_rails.dialects import supported_warehouses
from semantic_rails.runtime import Runtime
from semantic_rails.runtime_parts.assumptions import split_hidden
from semantic_rails.runtime_parts.zero_filled import zero_filled_groups_notes

SHOP = Path(__file__).resolve().parent / "shop"

SIGNUPS = "measure.shop.signup_count"
ORDERS = "measure.shop.order_count"
REVENUE = "measure.shop.revenue"
HUGE_ORDERS = "measure.shop.huge_order_count"
REFUNDS = "measure.shop.refund_count"
CUSTOMER = "dimension.shop_customer_id"
STORE = "dimension.shop_order_store_id"
BY_CUSTOMER = {"select": [SIGNUPS, ORDERS, REVENUE], "group_by": [CUSTOMER]}
MARKS = {"signup_count": ["m1_absent"], "order_count": ["m2_absent"], "revenue": ["m3_absent"]}

# Customers with a signup and no order, and with an order and no signup.
NO_ORDERS = """SELECT COUNT(*) FROM signups AS s
  WHERE NOT EXISTS (SELECT 1 FROM orders AS o WHERE o.customer_id = s.customer_id)"""
NO_SIGNUP = """SELECT COUNT(DISTINCT o.customer_id) FROM orders AS o
  WHERE NOT EXISTS (SELECT 1 FROM signups AS s WHERE s.customer_id = o.customer_id)"""
CUSTOMERS = """SELECT COUNT(*) FROM (
  SELECT customer_id FROM signups UNION SELECT customer_id FROM orders) AS customers"""


def _notes(result: dict) -> list[dict]:
    return [row for row in result.get("warnings", []) if row["code"] == "ZERO_FILLED_GROUPS"]


def _having(output: str) -> str:
    return json.dumps({"field": output, "op": "not", "value": 0})


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_counts_the_customers_a_count_or_sum_reads_0_for_want_of_rows(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    [[no_orders]], [[no_signup]], [[customers]] = (
        source.reference(sql) for sql in (NO_ORDERS, NO_SIGNUP, CUSTOMERS)
    )
    assert (no_orders, no_signup, customers) == (1, 1, 8)

    result = runtime.query(BY_CUSTOMER)

    rows = {row[CUSTOMER]: row for row in result["rows"]}
    assert (rows[107]["order_count"], rows[108]["signup_count"]) == (0, 0)
    assert Decimal(str(rows[107]["revenue"])) == 0
    # Customer 105's one order has no amount: rows of unknown value, NULL, and never counted.
    assert rows[105]["revenue"] is None
    assert all(
        set(row) == {CUSTOMER, "signup_count", "order_count", "revenue"} for row in rows.values()
    )
    assert _notes(result) == [
        {
            "code": "ZERO_FILLED_GROUPS",
            "message": (
                "1 of 8 groups has no rows of entity.shop_customer in this call, so its "
                f"signup_count is 0 (to leave it out, add {_having('signup_count')} to having); "
                "1 of 8 groups has no rows of entity.shop_order in this call, so its order_count "
                f"and revenue are 0 (to leave it out, add {_having('order_count')} to having). "
                "A 0 there is data of nothing, not missing data."
            ),
            "severity": "info",
            "stage": "execution",
            "details": {
                "outputs": [
                    {
                        "output": "signup_count",
                        "zero_filled": no_signup,
                        "entities": ["entity.shop_customer"],
                        "having": {"field": "signup_count", "op": "not", "value": 0},
                    },
                    {
                        "output": "order_count",
                        "zero_filled": no_orders,
                        "entities": ["entity.shop_order"],
                        "having": {"field": "order_count", "op": "not", "value": 0},
                    },
                    {
                        "output": "revenue",
                        "zero_filled": no_orders,
                        "entities": ["entity.shop_order"],
                        "having": {"field": "revenue", "op": "not", "value": 0},
                    },
                ],
                "groups": customers,
                "truncated": False,
                "agent_action": "mention_when_interpreting_results",
            },
            "object_ids": [ORDERS, REVENUE, SIGNUPS],
        }
    ]

    # The condition the note gives leaves the customer out; the other output's group stays.
    kept = runtime.query({**BY_CUSTOMER, "having": [json.loads(_having("order_count"))]})
    assert sorted(row[CUSTOMER] for row in kept["rows"]) == [101, 102, 103, 104, 105, 106, 108]
    [note] = _notes(kept)
    assert [row["output"] for row in note["details"]["outputs"]] == ["signup_count"]

    # Total rows are the call at a coarser grain, never one of its groups.
    totaled = runtime.query({**BY_CUSTOMER, "totals": []})
    assert len(totaled["rows"]) == customers + 1
    assert _notes(totaled) == _notes(result)


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_says_nothing_when_every_group_has_rows_of_each_measure(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    call = {
        "select": [REVENUE, REFUNDS],
        "group_by": [STORE],
        "where": [{"field": STORE, "op": "is", "value": ["a", "b"]}],
    }
    result = runtime.query(call)
    reference = source.reference(
        "SELECT o.store_id, COUNT(r.refund_id) FROM orders AS o JOIN refunds AS r "
        "ON r.order_id = o.order_id WHERE o.store_id IN ('a', 'b') GROUP BY 1 ORDER BY 1"
    )
    assert [(row[STORE], row["refund_count"]) for row in result["rows"]] == reference
    assert _notes(result) == []
    # Every store, the one with no store included: that one has no refunds.
    [note] = _notes(runtime.query({**call, "where": []}))
    assert note["message"].startswith("1 of 3 groups has no rows of entity.shop_refund in this")


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_group_that_reads_null_is_never_counted(request, backend):
    """No order reaches 1000, so the large-order count has no data in scope and reads NULL in
    every group, customer 107's included: NO_DATA_IN_SCOPE's, never a zero-filled group."""
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    [[large]] = source.reference("SELECT COUNT(*) FROM orders WHERE amount >= 1000")
    assert large == 0

    result = runtime.query({"select": [SIGNUPS, HUGE_ORDERS], "group_by": [CUSTOMER]})

    assert all(row["huge_order_count"] is None for row in result["rows"])
    codes = [row["code"] for row in result["warnings"]]
    assert "NO_DATA_IN_SCOPE" in codes
    [note] = _notes(result)
    assert [row["output"] for row in note["details"]["outputs"]] == ["signup_count"]


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_either_observation_scope_notes_the_groups_it_fills(request, backend):
    """Under either scope, orders have data in the call's scope, so customer 107's empty group
    reads 0 and the note says so. Only for customer 107 alone does ``query`` find no order in
    scope: there the order outputs read NULL with NO_DATA_IN_SCOPE, and nothing is noted."""
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    alone = {**BY_CUSTOMER, "where": [{"field": CUSTOMER, "op": "is", "value": 107}]}
    order_outputs = ["order_count", "revenue"]

    for scope in ("dataset", "query"):
        result = runtime.query({**BY_CUSTOMER, "observation_scope": scope})
        assert result["observation_scope"] == scope
        [note] = _notes(result)
        assert [row["output"] for row in note["details"]["outputs"]] == [
            "signup_count",
            *order_outputs,
        ]

    [filled] = runtime.query({**alone, "observation_scope": "dataset"})["rows"]
    assert (filled["order_count"], Decimal(str(filled["revenue"]))) == (0, 0)
    queried = runtime.query({**alone, "observation_scope": "query"})
    [row] = queried["rows"]
    assert (row["order_count"], row["revenue"]) == (None, None)
    [no_data] = [row for row in queried["warnings"] if row["code"] == "NO_DATA_IN_SCOPE"]
    assert no_data["details"]["outputs"] == order_outputs
    assert _notes(queried) == []


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_truncated_answer_counts_the_rows_shown_as_a_lower_bound(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    call = {
        "select": [SIGNUPS, ORDERS],
        "group_by": [CUSTOMER],
        "order_by": [
            {"field": "order_count", "direction": "asc"},
            {"field": CUSTOMER, "direction": "asc"},
        ],
    }

    result = runtime.query(call, limits={"max_rows": 3})

    assert result["truncated"] is True
    assert [row[CUSTOMER] for row in result["rows"]] == [107, 104, 105]
    [note] = _notes(result)
    assert note["message"] == (
        "At least 1 group (1 of the 3 rows shown) has no rows of entity.shop_order in this call, "
        f"so its order_count is 0 (to leave it out, add {_having('order_count')} to having). "
        "A 0 there is data of nothing, not missing data."
    )
    assert (note["details"]["groups"], note["details"]["truncated"]) == (3, True)


def test_the_note_reads_no_other_query(duckdb_backend, monkeypatch):
    runtime = duckdb_backend.runtimes["utc_authored"]
    statements: list[str] = []
    real = runtime_module._adapter_query

    def counted(adapter, query, **kwargs):
        statements.append(str(getattr(query, "sql", query)))
        return real(adapter, query, **kwargs)

    monkeypatch.setattr(runtime_module, "_adapter_query", counted)
    result = runtime.query(BY_CUSTOMER)

    assert _notes(result)
    assert statements == [result["rendered_sql"]]


@pytest.fixture(scope="module")
def shop() -> Any:
    return load_package_config(str(SHOP))


def _on(config: Any, warehouse: str) -> Any:
    return replace(config, package=replace(config.package, warehouse=warehouse))


def _compiled(config: Any, query: dict[str, Any]) -> dict[str, Any]:
    """The statement the runtime runs for ``query`` (no warehouse is opened)."""
    runtime = Runtime.from_config(config, source_path=str(SHOP), package_id="shop")
    try:
        payload = runtime.engine_payload(query, now="2026-10-01T00:00:00Z")
        return compile_query(runtime.config, runtime.registry, payload)
    finally:
        runtime.close()


@pytest.mark.parametrize("warehouse", sorted(supported_warehouses()))
def test_every_warehouse_marks_the_groups_a_measure_never_reached(shop, warehouse):
    config = _on(shop, warehouse)
    compiled = _compiled(config, BY_CUSTOMER)

    assert compiled["absent_rows"] == MARKS
    guard = next(cte for cte in compiled["sql_ast"].ctes if cte.name == "guarded_base")
    assert [field.alias for field in guard.query.select][-3:] == [
        "m1_absent",
        "m2_absent",
        "m3_absent",
    ]
    # A count's mark reads its value, a sum's its row count: NULL only where no row reached.
    sql = " ".join(compiled["sql"].split()).casefold()
    for source, mark in (("m1", "m1_absent"), ("m2", "m2_absent"), ("m3_rows", "m3_absent")):
        assert f"when (base.{source} is null) then 1 else 0 end as" in sql, mark

    # A warehouse that folds unquoted names returns the marks in its own case; the answer
    # never shows them, and the note counts the rows they mark.
    returned = QueryRows(
        [
            {CUSTOMER: 101, "signup_count": 1, "order_count": 3, "revenue": 23}
            | {"M1_ABSENT": 0, "M2_ABSENT": 0, "M3_ABSENT": 0},
            {CUSTOMER: 107, "signup_count": 1, "order_count": 0, "revenue": 0}
            | {"M1_ABSENT": 0, "M2_ABSENT": 1, "M3_ABSENT": 1},
            {CUSTOMER: 108, "signup_count": 0, "order_count": 1, "revenue": 2}
            | {"M1_ABSENT": "1", "M2_ABSENT": 0, "M3_ABSENT": 0},
        ],
        truncated=False,
    )
    rows, hidden = split_hidden(returned, compiled)
    assert [set(row) for row in rows] == [{CUSTOMER, "signup_count", "order_count", "revenue"}] * 3
    [note] = zero_filled_groups_notes(config, compiled, rows, hidden)
    assert [(row["output"], row["zero_filled"]) for row in note["details"]["outputs"]] == [
        ("signup_count", 1),
        ("order_count", 1),
        ("revenue", 1),
    ]
    assert note["details"]["groups"] == 3


def test_a_marked_group_that_reads_null_is_no_zero_filled_group(shop):
    """The mark says no row reached the group; a NULL there is no data, not data of nothing."""
    compiled = _compiled(shop, BY_CUSTOMER)
    returned = [
        {CUSTOMER: 107, "signup_count": 1, "order_count": None, "revenue": None}
        | {"m1_absent": 0, "m2_absent": 1, "m3_absent": 1},
    ]
    rows, hidden = split_hidden(returned, compiled)
    assert rows == [{CUSTOMER: 107, "signup_count": 1, "order_count": None, "revenue": None}]
    assert zero_filled_groups_notes(shop, compiled, rows, hidden) == []


def test_a_withheld_output_or_a_mark_that_never_came_back_counts_nothing(shop):
    """A policy that withholds an output's values hears nothing of its groups either, and an
    output whose mark the warehouse never returned is never guessed at."""
    compiled = _compiled(shop, BY_CUSTOMER)
    returned = [
        {CUSTOMER: 107, "signup_count": 1, "order_count": 0, "revenue": 0}
        | {"m1_absent": 0, "m2_absent": 1},
    ]
    rows, hidden = split_hidden(returned, compiled)
    [note] = zero_filled_groups_notes(shop, compiled, rows, hidden)
    assert [row["output"] for row in note["details"]["outputs"]] == ["order_count"]
    assert note["object_ids"] == [ORDERS]
    withheld = zero_filled_groups_notes(
        shop, compiled, rows, hidden, excluded_outputs={"order_count"}
    )
    assert withheld == []


CALLS = {
    "a series": {
        **BY_CUSTOMER,
        "time": {"grain": "month", "temporal_role": "temporal_role.shop_order_ordered_at"},
    },
    "one leaf": {"select": [REVENUE, ORDERS], "group_by": [STORE]},
    "no group": {"select": [SIGNUPS, ORDERS]},
}


@pytest.mark.parametrize("query", CALLS.values(), ids=CALLS.keys())
def test_a_call_that_holds_no_such_group_carries_no_mark(shop, query):
    compiled = _compiled(shop, query)
    assert compiled["absent_rows"] == {}
    assert "_absent" not in compiled["sql"]
