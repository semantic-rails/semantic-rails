"""Regression tests for silent-wrong-result ``where`` filter lowering.

Covers confirmed bugs, in the filter words' terms:

1. A null value compared with an op rendered ``col != NULL`` (always UNKNOWN in SQL
   three-valued logic: silent zero rows). A null value is now refused for every word
   except ``missing``, which says "has no value" (``IS NULL``) or "has one"
   (``IS NOT NULL``).
2. A scalar membership value character-split (``'Philadelphia'`` became
   ``IN ('P', 'h', 'i', ...)``). ``is`` / ``not`` take a scalar or a list; a scalar and a
   one-element list are the same value.
3. Null-test and pattern spellings the runtime rejected with an opaque renderer error. They
   are refused at the op with the word to write instead (``missing``, ``contains``).
4. A list value with a single-value op rendered as one string literal and silently matched
   no rows. A list is refused for every word but ``is`` / ``not``.

``not`` keeps the rows whose value is unknown: ``(x <> v OR x IS NULL)``.

The shared mapping lives in ``sql_ast.build_filter_condition`` so every filter lowering site
(query ``where[]``, measure-bound filters, result filters, predicate sources) behaves
identically.
"""

from __future__ import annotations

import duckdb
import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.renderer import render_expr
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlIdentifier,
    SqlLiteral,
    build_comparison_condition,
    build_filter_condition,
    filter_rejects_null,
)

STORE = "dimension.jaffle_store_name"
ORDER_NUMBER = "dimension.jaffle_order_customer_order_number"
NEW_CUSTOMER = "dimension.jaffle_order_is_new_customer_order"

# ---------------------------------------------------------------------
# Unit checks on the consolidated helper.
# ---------------------------------------------------------------------

_COL = SqlIdentifier(parts=["t", "c"])


@pytest.mark.parametrize(
    "op,value,expected",
    [(op, "x", True) for op in ("is", "gt", "gte", "lt", "lte", "contains", "=", "IN", ">")]
    + [(op, ["x"], True) for op in ("is", "in")]
    + [(op, "x", False) for op in ("not", "!=", "<>", "NOT IN", " not   in ")]
    + [("missing", True, False), ("missing", False, True)]
    + [("unknown", "x", False), ("like", "x", False), ("AND", True, False)],
)
def test_filter_rejects_null_classifies_only_known_excluding_forms(op, value, expected):
    assert filter_rejects_null(op, value) is expected


# Every operand once, rotating the spelling of IS and the builder.
@pytest.mark.parametrize(
    ("op", "value", "builder"),
    [
        (["IS", "IS NOT", "is", " is   not "][i % 4], value, ["comparison", "binary"][i % 2])
        for i, value in enumerate(["x", "true", "false", 0, 1, 1.0, [], [True], {}])
    ],
)
def test_an_expression_is_refuses_non_null_non_boolean_operands(op, value, builder):
    """Inside the expression language ``IS`` keeps its SQL meaning: a null or boolean test."""
    with pytest.raises(SemanticLayerError) as exc:
        if builder == "comparison":
            build_comparison_condition(_COL, op, SqlLiteral(value))
        else:
            # Force the direct-construction bypass of filter lowering.
            SqlBinary(_COL, op, SqlLiteral(value))
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["op"] == op.strip().upper().replace("   ", " ")


@pytest.mark.parametrize(
    ("op", "value", "kept"),
    [
        ("is", True, [True]),
        ("is", False, [False]),
        ("not", True, [None, False]),
        ("not", False, [None, True]),
        ("missing", True, [None]),
        ("missing", False, [True, False]),
    ],
)
def test_boolean_words_execute_with_the_unknown_rule(op, value, kept):
    sql = render_expr(build_filter_condition(_COL, op, value))
    with duckdb.connect() as conn:
        rows = conn.execute(
            f"SELECT t.c FROM (VALUES (NULL), (TRUE), (FALSE)) t(c) WHERE {sql}"
        ).fetchall()
    assert rows == [(item,) for item in [None, True, False] if item in kept]


@pytest.mark.parametrize(("op", "value"), [("is", True), ("is", False), ("not", True)])
def test_boolean_words_execute_through_runtime_like_their_aliases(runtime_factory, op, value):
    runtime = runtime_factory("jaffle_shop")
    query = _query_with_where({"field": NEW_CUSTOMER, "op": op, "value": value})
    alias = _query_with_where(
        {"field": NEW_CUSTOMER, "op": "=" if op == "is" else "!=", "value": value}
    )
    try:
        assert runtime.validate(query)["ok"] is True
        assert runtime.query(query)["rows"] == runtime.query(alias)["rows"]
    finally:
        runtime.close()


def test_missing_lowers_to_null_tests():
    assert render_expr(build_filter_condition(_COL, "missing", True)) == "(t.c IS NULL)"
    assert render_expr(build_filter_condition(_COL, "missing", False)) == "t.c IS NOT NULL"


def test_engine_built_null_comparisons_are_null_tests():
    """The engine's own probes compare with null; a call never can (see the reader below)."""
    assert render_expr(build_filter_condition(_COL, "is", None)) == "(t.c IS NULL)"
    assert render_expr(build_filter_condition(_COL, "not", None)) == "t.c IS NOT NULL"


@pytest.mark.parametrize("op", ["gt", "gte", "lt", "lte", "contains"])
def test_a_null_value_is_refused_with_the_missing_word(op):
    with pytest.raises(SemanticLayerError) as exc:
        build_filter_condition(_COL, op, None)
    assert exc.value.code == "INVALID_QUERY"
    (hint,) = exc.value.details["recovery_hints"]
    assert hint["code"] == "USE_FILTER_VALUE_SHAPE"
    assert '"missing"' in hint["message"]


def test_is_with_a_scalar_or_one_value_list_is_one_equality():
    for value in ("Philadelphia", ["Philadelphia"]):
        assert render_expr(build_filter_condition(_COL, "is", value)) == "t.c = 'Philadelphia'"


def test_membership_lists_and_the_kept_unknowns_of_not():
    assert render_expr(build_filter_condition(_COL, "is", ["a", "b"])) == "(t.c IN ('a', 'b'))"
    assert render_expr(build_filter_condition(_COL, "not", "a")) == "(t.c <> 'a') OR (t.c IS NULL)"
    assert (
        render_expr(build_filter_condition(_COL, "not", ["a", "b"]))
        == "(t.c NOT IN ('a', 'b')) OR (t.c IS NULL)"
    )


def test_engine_built_empty_memberships_compile_to_constants():
    assert build_filter_condition(_COL, "is", []) == SqlLiteral(False)
    assert build_filter_condition(_COL, "not", []) == SqlLiteral(True)


@pytest.mark.parametrize(
    ("op", "value"),
    [
        (op, [["Philadelphia", "Brooklyn"], ("Philadelphia",)][i % 2])
        for i, op in enumerate(["gt", "gte", "lt", "lte", "contains"])
    ],
)
def test_list_value_with_single_value_word_is_rejected(op, value):
    with pytest.raises(SemanticLayerError) as exc:
        build_filter_condition(_COL, op, value)
    assert exc.value.code == "INVALID_QUERY"
    (hint,) = exc.value.details["recovery_hints"]
    assert hint["code"] == "USE_FILTER_VALUE_SHAPE"
    assert "'is' / 'not'" in hint["message"]


# ---------------------------------------------------------------------
# The call's reader: one canonical value per word.
# ---------------------------------------------------------------------


def _query_with_where(where_item: dict) -> dict:
    return {
        "select": [{"expression": "measure.jaffle.revenue_usd", "as": "revenue"}],
        "where": [where_item],
    }


@pytest.mark.parametrize(("op", "value"), [("is", ["Philadelphia"]), ("in", "Philadelphia")])
def test_reading_a_one_value_membership_keeps_one_value(op, value):
    engine = read_call(_query_with_where({"field": STORE, "op": op, "value": value}))[0]
    assert engine["where"] == [{"field": STORE, "op": "is", "value": "Philadelphia"}]


@pytest.mark.parametrize(
    ("op", "value", "path", "fix"),
    [
        ("is", None, "where[0].value", '"missing"'),
        ("not", [], "where[0].value", '"missing"'),
        ("missing", [1, 2], "where[0].value", "missing: true"),
        ("IS NULL", None, "where[0].op", '{"op": "missing", "value": true}'),
        ("IS NOT NULL", None, "where[0].op", '{"op": "missing", "value": false}'),
        ("LIKE", "Phil%", "where[0].op", '{"op": "starts_with", "value": "Phil"}'),
        ("NOT LIKE", "Phil%", "where[0].op", '{"op": "not_starts_with", "value": "Phil"}'),
    ],
)
def test_a_retired_or_null_spelling_is_refused_at_its_path(op, value, path, fix):
    with pytest.raises(SemanticLayerError) as exc:
        read_call(_query_with_where({"field": STORE, "op": op, "value": value}))
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == path
    (hint,) = exc.value.details["recovery_hints"]
    assert fix in hint["message"]


# ---------------------------------------------------------------------
# Runtime-level check — validate() accepts every word and alias the schema lists.
# ---------------------------------------------------------------------


def test_validate_accepts_every_filter_word_and_alias(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    cases = [
        {"op": "is", "value": "Philadelphia"},
        {"op": "not", "value": "Philadelphia"},
        {"op": "is", "value": ["Philadelphia", "Brooklyn"]},
        {"op": "not", "value": ["Philadelphia"]},
        {"op": "gte", "value": "B"},
        {"op": "lt", "value": "Q"},
        {"op": "contains", "value": "phil"},
        {"op": "missing", "value": True},
        {"op": "missing", "value": False},
        {"op": "=", "value": "Philadelphia"},
        {"op": "!=", "value": "Philadelphia"},
        {"op": "IN", "value": ["Philadelphia"]},
        {"op": "NOT IN", "value": ["Philadelphia"]},
    ]
    try:
        for case in cases:
            report = runtime.validate(_query_with_where({"field": STORE, **case}))
            assert report["ok"] is True, f"{case['op']!r} failed validate: {report.get('errors')}"
    finally:
        runtime.close()


def _validate(runtime, **clauses) -> dict:
    return runtime.validate(
        {
            "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
            "group_by": [STORE],
            **clauses,
        }
    )


def test_a_list_reads_as_membership_not_a_string(runtime_factory):
    # A list once compiled to store_name = '[''Philadelphia'', ''Brooklyn'']' (zero rows).
    runtime = runtime_factory("jaffle_shop")
    stores = ["Philadelphia", "Brooklyn"]
    try:
        both = runtime.query(
            {
                "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
                "group_by": [STORE],
                "where": [{"field": STORE, "op": "is", "value": stores}],
            }
        )["rows"]
    finally:
        runtime.close()
    assert sorted(row[STORE] for row in both) == sorted(stores)


@pytest.mark.parametrize(("field", "value"), [(STORE, [1, 2]), (ORDER_NUMBER, ["bad", 2])])
def test_wrong_type_list_gets_the_value_type_hint_on_public_paths(runtime_factory, field, value):
    runtime = runtime_factory("jaffle_shop")
    query = {
        "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
        "group_by": [STORE],
        "where": [{"field": field, "op": "is", "value": value}],
    }
    try:
        report = runtime.validate(query)
        assert report["ok"] is False
        issue = report["errors"][0]
        assert issue["code"] == "INVALID_QUERY"
        assert [hint["kind"] for hint in issue["recovery_hints"]] == ["fix_filter_value_type"]
        for method in (runtime.compile, runtime.query):
            with pytest.raises(SemanticLayerError) as exc:
                method(query)
            assert exc.value.code == "INVALID_QUERY"
    finally:
        runtime.close()


def test_numeric_membership_lists_and_scalar_type_diagnostics_stay_intact(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        for op in ("is", "not"):
            query = {
                "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
                "group_by": [STORE],
                "where": [{"field": ORDER_NUMBER, "op": op, "value": [1, 2]}],
            }
            assert runtime.validate(query)["ok"]
            assert runtime.compile(query)["ok"]
            assert runtime.query(query)["ok"]
        for value in ("bad", ["bad", 2]):
            report = _validate(runtime, where=[{"field": ORDER_NUMBER, "op": "is", "value": value}])
            assert report["ok"] is False
            issue = report["errors"][0]
            assert issue["code"] == "INVALID_QUERY"
            assert "expects a numeric value" in issue["message"]
            assert [hint["kind"] for hint in issue["recovery_hints"]] == ["fix_filter_value_type"]
        string_report = _validate(runtime, where=[{"field": STORE, "op": "is", "value": 1}])
        assert string_report["ok"] is False
        assert "expects a string value" in string_report["errors"][0]["message"]
        hints = string_report["errors"][0]["recovery_hints"]
        assert [hint["kind"] for hint in hints] == ["fix_filter_value_type"]
        assert "on text; in YAML, quote text" in hints[0]["message"]
    finally:
        runtime.close()


def test_validate_rejects_a_list_in_a_result_filter_comparison(runtime_factory):
    # Used to validate, then fail in the warehouse on the stringified list.
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _validate(runtime, having=[{"field": "orders", "op": "gt", "value": [1, 2]}])
        assert report["ok"] is False
        issue = report["errors"][0]
        assert issue["code"] == "INVALID_QUERY"
        assert issue["details"]["path"] == "having[0].value"
    finally:
        runtime.close()
