"""ZERO_FILLED_GROUPS: the groups whose count or sum reads 0 because none of its own rows reach
them, while another measure's rows hold the group, checked against reference SQL.

Customers are parents with some children missing: 107 signed up and never ordered, and 108
ordered without signing up. Grouped by customer beside each other, signups and orders join on
the customer, so 107 reads 0 orders and 108 reads 0 signups (data of nothing, not missing data).
The statement marks those groups itself; the note reads the marks back, with no other query.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from semantic_rails import runtime as runtime_module

SIGNUPS = "measure.shop.signup_count"
ORDERS = "measure.shop.order_count"
REVENUE = "measure.shop.revenue"
HUGE_ORDERS = "measure.shop.huge_order_count"
REFUNDS = "measure.shop.refund_count"
CUSTOMER = "dimension.shop_customer_id"
STORE = "dimension.shop_order_store_id"
BY_CUSTOMER = {"select": [SIGNUPS, ORDERS, REVENUE], "group_by": [CUSTOMER]}

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
