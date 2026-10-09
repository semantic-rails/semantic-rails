"""The filter words on the shop fixture: what `not`, `missing` and `contains` keep, and what the
answer says about the unknown rows a `not` kept. Each answer is checked against independent SQL."""

from __future__ import annotations

import pytest

from .test_correctness import BACKENDS, _backend

CHANNEL = "dimension.shop_customer_channel"
STORE = "dimension.shop_order_store_id"
ORDERS = {"expression": "measure.shop.order_count", "as": "orders"}
REVENUE = "measure.shop.revenue"
# The orders whose looked-up channel is unknown: 7 (customer 105, no channel) and 11 (customer
# 108, never signed up).
CHANNEL_OF = "(SELECT s.channel FROM signups AS s WHERE s.customer_id = o.customer_id)"


def _runtime(request, backend_name):
    return _backend(request, backend_name).runtimes["utc_authored"]


def _reference(request, backend_name, sql):
    return _backend(request, backend_name).reference(sql)


def _warnings(result, code):
    return [row for row in result["warnings"] if row["code"] == code]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_not_keeps_unknown_rows_and_says_how_many(request, backend_name):
    runtime = _runtime(request, backend_name)
    result = runtime.query(
        {
            "select": [REVENUE, ORDERS],
            "group_by": [CHANNEL],
            "where": [{"field": CHANNEL, "op": "not", "value": "web"}],
        }
    )
    rows = {row[CHANNEL]: row["orders"] for row in result["rows"]}
    assert rows == {"store": 3, None: 2}
    [kept] = _reference(
        request,
        backend_name,
        f"SELECT COUNT(*) FROM orders AS o WHERE {CHANNEL_OF} IS NULL",
    )
    [warning] = _warnings(result, "UNKNOWN_ROWS_KEPT")
    assert warning["details"] == {"field": CHANNEL, "rows": kept[0], "values": [None]}
    assert kept[0] == 2
    assert '"op": "missing", "value": false' in warning["message"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_not_beside_missing_false_leaves_unknowns_out_and_says_nothing(request, backend_name):
    runtime = _runtime(request, backend_name)
    result = runtime.query(
        {
            "select": [ORDERS],
            "where": [
                {"field": CHANNEL, "op": "not", "value": "web"},
                {"field": CHANNEL, "op": "missing", "value": False},
            ],
        }
    )
    [expected] = _reference(
        request,
        backend_name,
        f"SELECT COUNT(*) FROM orders AS o WHERE {CHANNEL_OF} <> 'web'",
    )
    assert [row["orders"] for row in result["rows"]] == [expected[0]] == [3]
    assert not _warnings(result, "UNKNOWN_ROWS_KEPT")


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_not_with_no_unknown_rows_in_scope_says_nothing(request, backend_name):
    # Store b's orders all have a store, so a `not` on the store keeps no unknown row there.
    runtime = _runtime(request, backend_name)
    result = runtime.query(
        {
            "select": [REVENUE],
            "where": [
                {"field": STORE, "op": "not", "value": "a"},
                {"field": STORE, "op": "is", "value": "b"},
            ],
        }
    )
    [expected] = _reference(
        request,
        backend_name,
        "SELECT CAST(SUM(amount) AS INTEGER) FROM orders WHERE store_id = 'b'",
    )
    assert [int(row["revenue"]) for row in result["rows"]] == [expected[0]] == [25]
    assert not _warnings(result, "UNKNOWN_ROWS_KEPT")


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_not_counts_unknown_rows_under_the_query_window(request, backend_name):
    # Order 8 (store NULL) falls in May 2024; a window ending in April keeps no unknown store.
    runtime = _runtime(request, backend_name)
    call = {
        "select": [REVENUE],
        "time": {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "year"},
        "where": [{"field": STORE, "op": "not", "value": "a"}],
    }
    april = runtime.query({**call, "time": {**call["time"], "end": "2024-05-01"}})
    assert not _warnings(april, "UNKNOWN_ROWS_KEPT")
    whole = runtime.query(call)
    [kept] = _reference(request, backend_name, "SELECT COUNT(*) FROM orders WHERE store_id IS NULL")
    [warning] = _warnings(whole, "UNKNOWN_ROWS_KEPT")
    assert warning["details"]["rows"] == kept[0] == 1


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(("missing", "condition"), [(True, "IS NULL"), (False, "IS NOT NULL")])
def test_missing_matches_only_unknown_or_only_known_rows(request, backend_name, missing, condition):
    runtime = _runtime(request, backend_name)
    result = runtime.query(
        {"select": [ORDERS], "where": [{"field": STORE, "op": "missing", "value": missing}]}
    )
    [expected] = _reference(
        request, backend_name, f"SELECT COUNT(*) FROM orders WHERE store_id {condition}"
    )
    assert [row["orders"] for row in result["rows"]] == [expected[0]]


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(("text", "expected"), [("WE", 1), ("tor", 1), ("%", 0), ("_", 0)])
def test_contains_is_a_case_insensitive_literal_match(request, backend_name, text, expected):
    runtime = _runtime(request, backend_name)
    result = runtime.query(
        {
            "select": [{"expression": "measure.shop.signup_count", "as": "signups"}],
            "group_by": [CHANNEL],
            "where": [{"field": CHANNEL, "op": "contains", "value": text}],
        }
    )
    escaped = text.lower().replace("%", "\\%").replace("_", "\\_")
    rows = _reference(
        request,
        backend_name,
        "SELECT channel, COUNT(DISTINCT customer_id) FROM signups "
        f"WHERE LOWER(channel) LIKE '%{escaped}%' ESCAPE '\\' GROUP BY 1",
    )
    assert len(result["rows"]) == len(rows) == expected
    assert {row[CHANNEL]: row["signups"] for row in result["rows"]} == dict(rows)
