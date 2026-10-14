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
    return [row for row in result.get("warnings", []) if row["code"] == code]


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


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_an_upper_bound_on_the_clock_date_keeps_a_running_total(request, backend_name):
    """`lt` on the clock's date is an upper bound, which a running total can take; `gte` cuts
    the history it sums and is refused."""
    runtime = _runtime(request, backend_name)
    call = {
        "select": [{"expression": {"kind": "cumulative", "input": REVENUE}, "as": "history"}],
        "time": {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "month"},
    }
    bound = {"field": "dimension.shop_order_ordered_at", "value": "2024-01-01"}
    result = runtime.query({**call, "where": [{**bound, "op": "lt"}]})
    expected = _reference(
        request,
        backend_name,
        "SELECT CAST(SUM(SUM(amount)) OVER (ORDER BY date_trunc('month', ordered_at)) AS INTEGER) "
        "FROM orders WHERE ordered_at < TIMESTAMP '2024-01-01' "
        "GROUP BY date_trunc('month', ordered_at) ORDER BY date_trunc('month', ordered_at)",
    )
    assert [int(row["history"]) for row in result["rows"]] == [row[0] for row in expected]
    assert [row[0] for row in expected] == [15, 22]
    report = runtime.validate({**call, "where": [{**bound, "op": "gte"}]})
    assert report["errors"][0]["details"]["start"] == "2024-01-01"


NAME = "dimension.shop_customer_name"
SIGNUPS = {"expression": "measure.shop.signup_count", "as": "signups"}


def _signups(runtime, where):
    result = runtime.query({"select": [SIGNUPS], "where": where})
    return [row["signups"] for row in result["rows"]], result


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    ("op", "text", "pattern"),
    [
        ("starts_with", "A", "a%"),
        ("starts_with", "an", "an%"),
        ("ends_with", "A", "%a"),
        ("ends_with", "N", "%n"),
        ("contains", "A", "%a%"),
    ],
)
def test_a_prefix_or_suffix_word_keeps_only_its_shape_ignoring_case(
    request, backend_name, op, text, pattern
):
    # Names: Ann (101, 103), Bea, Cy, Di, Ed, Flo. "a" starts two names, ends one, is in three.
    runtime = _runtime(request, backend_name)
    found, _ = _signups(runtime, [{"field": NAME, "op": op, "value": text}])
    [expected] = _reference(
        request,
        backend_name,
        f"SELECT COUNT(DISTINCT customer_id) FROM signups WHERE LOWER(name) LIKE '{pattern}'",
    )
    assert found == [expected[0]]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_starts_with_is_narrower_than_contains_on_the_same_text(request, backend_name):
    runtime = _runtime(request, backend_name)
    counts = {
        op: _signups(runtime, [{"field": NAME, "op": op, "value": "a"}])[0]
        for op in ("starts_with", "ends_with", "contains")
    }
    assert counts == {"starts_with": [2], "ends_with": [1], "contains": [3]}


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("op", ["starts_with", "ends_with", "not_starts_with", "not_ends_with"])
@pytest.mark.parametrize("text", ["%", "_"])
def test_wildcards_in_a_text_word_are_literal(request, backend_name, op, text):
    # As LIKE wildcards, '%' and '_' would match every name; no name holds either.
    runtime = _runtime(request, backend_name)
    found, _ = _signups(runtime, [{"field": NAME, "op": op, "value": text}])
    assert found == ([7] if op.startswith("not_") else [0])


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    ("op", "text", "known"),
    [
        ("not_contains", "E", "NOT LIKE '%e%'"),
        ("not_starts_with", "W", "NOT LIKE 'w%'"),
        ("not_ends_with", "RE", "NOT LIKE '%re'"),
    ],
)
def test_a_not_text_word_keeps_unknown_rows_and_says_how_many(
    request, backend_name, op, text, known
):
    # Order 7's customer (105) has no channel and order 11's (108) never signed up.
    runtime = _runtime(request, backend_name)
    result = runtime.query(
        {
            "select": [ORDERS],
            "group_by": [CHANNEL],
            "where": [{"field": CHANNEL, "op": op, "value": text}],
        }
    )
    rows = {row[CHANNEL]: row["orders"] for row in result["rows"]}
    expected = _reference(
        request,
        backend_name,
        f"SELECT {CHANNEL_OF}, COUNT(*) FROM orders AS o "
        f"WHERE LOWER({CHANNEL_OF}) {known} OR {CHANNEL_OF} IS NULL GROUP BY 1",
    )
    assert rows == dict(expected)
    assert rows[None] == 2
    [warning] = _warnings(result, "UNKNOWN_ROWS_KEPT")
    assert warning["details"] == {"field": CHANNEL, "rows": 2, "values": [None]}
    assert warning["message"].startswith(f"'{op}' keeps rows whose {CHANNEL} is unknown")
    assert '"op": "missing", "value": false' in warning["message"]
    # Beside missing: false, the unknown rows are left out and nothing is said.
    known_only = runtime.query(
        {
            "select": [ORDERS],
            "where": [
                {"field": CHANNEL, "op": op, "value": text},
                {"field": CHANNEL, "op": "missing", "value": False},
            ],
        }
    )
    [[count]] = _reference(
        request, backend_name, f"SELECT COUNT(*) FROM orders AS o WHERE LOWER({CHANNEL_OF}) {known}"
    )
    assert [row["orders"] for row in known_only["rows"]] == [count]
    assert not _warnings(known_only, "UNKNOWN_ROWS_KEPT")


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(("op", "expected"), [("starts_with", 3), ("not_starts_with", 4)])
def test_a_comparison_takes_a_text_word_with_its_meaning(request, backend_name, op, expected):
    # Web signups are 101, 103 and 106; not_starts_with also keeps 105, which has no channel.
    runtime = _runtime(request, backend_name)
    condition = {
        "kind": "comparison",
        "op": op,
        "left": {"kind": "column", "column": "channel", "entity": "entity.shop_customer"},
        "right": {"kind": "literal", "value": "W"},
    }
    expression = {"kind": "aggregate_if", "aggregation": "count", "condition": condition}
    result = runtime.query({"select": [{"expression": expression, "as": "n"}]})
    assert [row["n"] for row in result["rows"]] == [expected]
