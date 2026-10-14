"""The filter words on the shop fixture: what `not`, `missing` and `contains` keep, and what the
answer says about the unknown rows a `not` kept. Each answer is checked against independent SQL."""

from __future__ import annotations

import shutil

import pytest

from semantic_rails.errors import SemanticLayerError

from .conftest import SHOP
from .conftest import _runtime as runtime_for
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


# --- A text word reads a dimension, in every place it is written ---------------------------

CHANNEL_COLUMN = {"kind": "column", "column": "channel", "entity": "entity.shop_customer"}


def _count_if(op, text, column=CHANNEL_COLUMN):
    condition = {
        "kind": "comparison",
        "op": op,
        "left": column,
        "right": {"kind": "literal", "value": text},
    }
    expression = {"kind": "aggregate_if", "aggregation": "count", "condition": condition}
    return {"select": [{"expression": expression, "as": "n"}]}


@pytest.fixture(scope="module")
def unknown_channel(tmp_path_factory):
    """The shop with customer 105's channel recorded as "N/A", which the dimension declares as
    unknown: every reader of the channel must read it as NULL."""
    package = tmp_path_factory.mktemp("unknown_channel") / "shop"
    shutil.copytree(SHOP, package)
    seed = package / "data" / "seed.sql"
    text = seed.read_text(encoding="utf-8")
    old = "(105, TIMESTAMP '2024-05-06 09:00:00', NULL)"
    assert text.count(old) == 1
    seed.write_text(text.replace(old, "(105, TIMESTAMP '2024-05-06 09:00:00', 'N/A')"))
    signups = package / "models" / "signups.yml"
    model = signups.read_text(encoding="utf-8")
    old = "domain: [web, store]}"
    assert model.count(old) == 1
    signups.write_text(model.replace(old, "domain: [web, store], unknown_values: [N/A]}"))
    runtime = runtime_for(package)
    yield runtime
    runtime.close()


@pytest.mark.parametrize(("op", "expected"), [("not_starts_with", 7), ("starts_with", 0)])
def test_a_comparison_text_word_reads_the_dimensions_unknown_values(unknown_channel, op, expected):
    # 105's "N/A" is unknown: not_starts_with "N" keeps it (with every other signup), and
    # starts_with "N" never matches it, exactly as the filter on the dimension reads it.
    by_filter = unknown_channel.query(
        {"select": [SIGNUPS], "where": [{"field": CHANNEL, "op": op, "value": "N"}]}
    )
    by_comparison = unknown_channel.query(_count_if(op, "N"))
    assert [row["signups"] for row in by_filter["rows"]] == [expected]
    assert [row["n"] for row in by_comparison["rows"]] == [expected]


@pytest.mark.parametrize(
    ("column", "says"),
    [
        ({"kind": "column", "column": "amount", "entity": "entity.shop_order"}, "no dimension"),
        (
            {"kind": "column", "column": "source_day", "entity": "entity.shop_clock_edge"},
            "several dimensions",
        ),
    ],
)
def test_a_comparison_text_word_on_a_column_no_one_dimension_declares_is_refused(
    duckdb_backend, column, says
):
    runtime = duckdb_backend.runtimes["utc_authored"]
    [error] = runtime.validate(_count_if("starts_with", "1", column))["errors"][:1]
    assert error["details"]["path"] == "select[0].expression.condition.left"
    assert says in error["message"]


ORDER_COUNT = "measure.shop.order_count"
NOT_TEXT = [
    (ORDER_COUNT, "dimension.shop_order_customer_id", "id"),
    (ORDER_COUNT, "dimension.shop_order_ordered_at", "timestamp"),
    ("measure.shop.seats", "dimension.shop_account_day_snapshot_day", "date"),
]


@pytest.mark.parametrize(("measure", "field", "data_type"), NOT_TEXT)
@pytest.mark.parametrize("op", ["starts_with", "not_contains"])
def test_a_text_word_on_a_value_that_is_not_text_is_refused_at_its_path(
    duckdb_backend, measure, field, data_type, op
):
    runtime = duckdb_backend.runtimes["utc_authored"]
    condition = {"field": field, "op": op, "value": "1"}
    expression = {"kind": "aggregate", "measure": measure, "filter": {"all": [condition]}}
    calls = {
        "where[0].op": {"select": [measure], "where": [condition]},
        "select[0].expression.filter.all[0].op": {
            "select": [{"expression": expression, "as": "n"}]
        },
    }
    for path, call in calls.items():
        report = runtime.validate(call)
        assert not report["ok"], (path, call)
        [error] = report["errors"][:1]
        assert error["details"]["path"] == path
        assert data_type in error["message"]
        assert "starts_with" not in error["recovery_hints"][0]["message"]


def test_a_comparison_text_word_on_a_timestamp_column_is_refused_at_its_path(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    column = {"kind": "column", "column": "ordered_at", "entity": "entity.shop_order"}
    [error] = runtime.validate(_count_if("starts_with", "2024", column))["errors"][:1]
    assert error["details"]["path"] == "select[0].expression.condition.left"
    assert "timestamp" in error["message"]


def _per_customer(op, value):
    over = {
        "kind": "entity_value",
        "entity": "entity.shop_customer",
        "input": REVENUE,
        "where": [{"kind": "value_filter", "op": op, "value": value}],
    }
    expression = {"kind": "distribution", "function": "percentile", "p": 0.5, "over": over}
    return {"select": [{"expression": expression, "as": "p50"}]}


@pytest.mark.parametrize("op", ["starts_with", "contains", "LIKE", "not ilike"])
def test_a_per_entity_value_filter_takes_only_words_that_compare_a_number(duckdb_backend, op):
    runtime = duckdb_backend.runtimes["utc_authored"]
    [error] = runtime.validate(_per_customer(op, "1%"))["errors"][:1]
    assert error["details"]["path"] == "select[0].expression.over.where[0].op"
    hint = error["recovery_hints"][0]["message"]
    assert "gt" in hint
    for word in ("starts_with", "ends_with", "contains"):
        assert word not in hint
    assert runtime.validate(_per_customer("gt", 1))["ok"]


AGGREGATE_SURFACES = {
    "entity-filter-having": (
        lambda op: {
            "select": [ORDER_COUNT],
            "where": [
                {
                    "entity": "entity.shop_customer",
                    "having": [{"field": ORDER_COUNT, "op": op, "value": "1%"}],
                    "over": "all_time",
                }
            ],
        },
        "where[0].having[0].op",
    ),
    "scoped-predicate": (
        lambda op: {
            "select": [
                {
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": ORDER_COUNT,
                        "predicates": [
                            {
                                "entity": "entity.shop_customer",
                                "input": ORDER_COUNT,
                                "op": op,
                                "value": "1%",
                                "over": "all_time",
                            }
                        ],
                    },
                    "as": "n",
                }
            ]
        },
        "select[0].expression.predicates[0].op",
    ),
    "having": (
        lambda op: {
            "select": [{"expression": ORDER_COUNT, "as": "orders"}],
            "group_by": [STORE],
            "having": [{"field": "orders", "op": op, "value": "1%"}],
        },
        "having[0].op",
    ),
}


@pytest.mark.parametrize("surface", AGGREGATE_SURFACES)
@pytest.mark.parametrize("op", ["LIKE", "ilike", "starts_with"])
def test_an_aggregate_comparison_hints_the_words_that_compare_a_number(duckdb_backend, surface, op):
    runtime = duckdb_backend.runtimes["utc_authored"]
    build, path = AGGREGATE_SURFACES[surface]
    [error] = runtime.validate(build(op))["errors"][:1]
    assert error["details"]["path"] == path
    hint = error["recovery_hints"][0]["message"]
    assert "gt" in hint
    for word in ("starts_with", "ends_with", "contains"):
        assert word not in hint


def test_a_package_authored_like_is_refused_with_the_word_for_its_pattern(tmp_path):
    """A measure's own comparison takes the same LIKE rule as a call's: a case-sensitive
    'A%' would miss store "a"."""
    package = tmp_path / "shop"
    shutil.copytree(SHOP, package)
    orders = package / "models" / "orders.yml"
    model = orders.read_text(encoding="utf-8")
    old = (
        "op: '=',\n        left: {kind: column, column: store_id}, right: {kind: literal, "
        "value: a}},\n        then: {kind: column, column: amount}"
    )
    assert model.count(old) == 1
    orders.write_text(
        model.replace(old, old.replace("op: '='", "op: LIKE").replace("a}}", "'A%'}}"))
    )
    with pytest.raises(SemanticLayerError) as caught:
        runtime_for(package).validate({"select": ["measure.shop.store_a_amount"]})
    hint = caught.value.details["recovery_hints"][0]["message"]
    assert (
        hint
        == 'Write {"op": "starts_with", "right": {"kind": "literal", "value": "A"}}, which ignores case.'
    )
