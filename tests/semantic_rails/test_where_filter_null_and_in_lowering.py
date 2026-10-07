"""Regression tests for silent-wrong-result ``where`` filter lowering.

Covers three confirmed bugs:

1. ``value: null`` with a non-``=`` op rendered ``col != NULL`` (always
   UNKNOWN in SQL three-valued logic — silent zero rows). Now ``!=`` /
   ``<>`` / ``IS NOT`` lower to ``IS NOT NULL``; ordering / LIKE ops
   against null are rejected with a structured INVALID_QUERY.
2. ``IN`` / ``NOT IN`` with a bare scalar string character-split the
   value (``'Philadelphia'`` became ``IN ('P', 'h', 'i', ...)``). Now a
   scalar is normalized to a one-element list.
3. The published schema advertises ``IS NULL`` / ``IS NOT NULL`` /
   ``NOT IN`` ops that the runtime rejected with an opaque renderer
   error. Now they lower end-to-end, and empty IN / NOT IN lists
   compile to constant FALSE / TRUE instead of erroring.
4. A list value with a single-value op (``=``, ``!=``, ``>``, ``LIKE``...)
   rendered as one string literal (``= '[''a'', ''b'']'``) and silently
   matched no rows. Now it is rejected with a pointer to ``IN``.

The shared mapping lives in ``sql_ast.build_filter_condition`` so every
filter lowering site (query ``where[]``, measure-bound filters, metric
filter envelopes, predicate sources) behaves identically.
"""

from __future__ import annotations

from dataclasses import replace

import duckdb
import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.compiler import compile_query
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlIdentifier,
    SqlIn,
    SqlIsNull,
    SqlLiteral,
    build_comparison_condition,
    build_filter_condition,
)

# ---------------------------------------------------------------------
# Unit checks on the consolidated helper.
# ---------------------------------------------------------------------

_COL = SqlIdentifier(parts=["t", "c"])


@pytest.mark.parametrize(
    "op,value,expected",
    [(op, "x", True) for op in ("=", "!=", "<>", "<", "<=", ">", ">=", "LIKE", "NOT LIKE")]
    + [(op, None, True) for op in ("!=", "<>", "IS NOT")]
    + [(op, ["x"], True) for op in ("IN", "NOT IN")]
    + [("IS NOT NULL", value, True) for value in (None, "ignored")]
    + [(" is   not null ", None, True), (" not   like ", "x", True), (" is   not ", None, True)]
    + [(op, None, False) for op in ("IS NULL", "is null", " IS   NULL ", "=", "IS", "is", None)]
    + [("IS NULL", "ignored", False)]
    + [(op, None, False) for op in ("<", "<=", ">", ">=", "LIKE", "NOT LIKE")]
    + [
        (op, value, False)
        for op in ("IS DISTINCT FROM", "IS NOT DISTINCT FROM", "<=>")
        for value in (None, "x")
    ]
    + [(op, value, False) for op in ("IS", "IS NOT") for value in (True, False)]
    + [
        (" is   not distinct from ", "x", False),
        ("unknown", "x", False),
        ("unknown", None, False),
        ("AND", True, False),
    ]
    + [(None, "x", True), ("", "x", True)],
)
def test_filter_rejects_null_classifies_only_known_excluding_forms(op, value, expected):
    from semantic_rails.sql_ast import filter_rejects_null

    assert filter_rejects_null(op, value) is expected


@pytest.mark.parametrize("op", ["IS", "IS NOT", "is", " is   not "])
@pytest.mark.parametrize("value", ["x", "true", "false", 0, 1, 1.0, [], [True], {}])
@pytest.mark.parametrize("builder", ["filter", "comparison", "binary"])
def test_is_refuses_non_null_non_boolean_operands(op, value, builder):
    with pytest.raises(SemanticLayerError) as exc:
        if builder == "filter":
            build_filter_condition(_COL, op, value)
        elif builder == "comparison":
            build_comparison_condition(_COL, op, SqlLiteral(value))
        else:
            # Force the direct-construction bypass of filter lowering.
            SqlBinary(_COL, op, SqlLiteral(value))
    assert exc.value.code == "INVALID_QUERY"
    hint = exc.value.details["recovery_hints"][0]
    assert hint["code"] == "USE_EQUALITY_FOR_SCALAR"
    assert "'=' / '!='" in hint["message"]


@pytest.mark.parametrize("warehouse", supported_warehouses())
@pytest.mark.parametrize("op", ["IS", "IS NOT"])
@pytest.mark.parametrize(
    "field", ["dimension.jaffle_order_customer_order_number", "dimension.jaffle_store_name"]
)
def test_is_operand_refusal_is_independent_of_backend_and_dimension(
    package_config_factory, warehouse, op, field
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    with pytest.raises(SemanticLayerError) as exc:
        _compiled_sql(config, {"field": field, "op": op, "value": "x"})
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["recovery_hints"][0]["code"] == "USE_EQUALITY_FOR_SCALAR"


@pytest.mark.parametrize("op", ["IS", "IS NOT"])
@pytest.mark.parametrize(
    "field",
    [
        "dimension.jaffle_order_customer_order_number",
        "dimension.jaffle_store_name",
        "dimension.jaffle_order_is_new_customer_order",
    ],
)
def test_is_operand_is_refused_on_public_paths_before_adapter_access(
    runtime_factory, monkeypatch, op, field
):
    runtime = runtime_factory("jaffle_shop")
    query = _query_with_where({"field": field, "op": op, "value": "x"})

    def unexpected_adapter():
        pytest.fail("An invalid IS operand must be refused before adapter access")

    try:
        monkeypatch.setattr(runtime, "_get_adapter", unexpected_adapter)
        report = runtime.validate(query)
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_QUERY"
        assert report["recovery_hints"][0]["code"] == "USE_EQUALITY_FOR_SCALAR"
        for method in (runtime.compile, runtime.query):
            with pytest.raises(SemanticLayerError) as exc:
                method(query)
            assert exc.value.code == "INVALID_QUERY"
            assert exc.value.details["recovery_hints"][0]["code"] == "USE_EQUALITY_FOR_SCALAR"
    finally:
        runtime.close()


@pytest.mark.parametrize("op", ["IS", "IS NOT"])
@pytest.mark.parametrize("value", [None, True, False])
def test_is_allowed_operands_execute_with_expected_null_semantics(op, value):
    from semantic_rails.renderer import render_expr

    sql = render_expr(build_filter_condition(_COL, op, value))
    with duckdb.connect() as conn:
        rows = conn.execute(
            f"SELECT t.c FROM (VALUES (NULL), (TRUE), (FALSE)) t(c) WHERE {sql}"
        ).fetchall()
    expected = [item for item in [None, True, False] if (item is value) == (op == "IS")]
    assert rows == [(item,) for item in expected]


@pytest.mark.parametrize("op", ["IS", "IS NOT"])
@pytest.mark.parametrize("value", [None, True, False])
def test_is_allowed_operands_execute_through_runtime(runtime_factory, op, value):
    runtime = runtime_factory("jaffle_shop")
    field = "dimension.jaffle_order_is_new_customer_order"
    query = _query_with_where({"field": field, "op": op, "value": value})
    reference = _query_with_where(
        {"field": field, "op": "=" if op == "IS" else "!=", "value": value}
    )
    try:
        assert runtime.validate(query)["ok"] is True
        assert runtime.query(query)["rows"] == runtime.query(reference)["rows"]
    finally:
        runtime.close()


def test_null_with_equality_ops_lowers_to_is_null():
    for op in ("=", "IS", "is"):
        condition = build_filter_condition(_COL, op, None)
        assert isinstance(condition, SqlIsNull)


def test_null_with_inequality_ops_lowers_to_is_not_null():
    for op in ("!=", "<>", "IS NOT", "is not"):
        condition = build_filter_condition(_COL, op, None)
        assert isinstance(condition, SqlBinary)
        assert condition.op == "IS NOT"
        assert isinstance(condition.right, SqlLiteral) and condition.right.value is None


@pytest.mark.parametrize("op", ["<", "<=", ">", ">=", "LIKE", "NOT LIKE"])
def test_null_with_ordering_or_like_ops_is_rejected(op):
    with pytest.raises(SemanticLayerError) as exc:
        build_filter_condition(_COL, op, None)
    assert exc.value.code == "INVALID_QUERY"
    hints = exc.value.details.get("recovery_hints", [])
    assert hints and hints[0]["code"] == "USE_NULL_TEST_OR_SCALAR"
    assert "IS NULL" in hints[0]["message"]


def test_explicit_null_test_ops_ignore_value():
    assert isinstance(build_filter_condition(_COL, "IS NULL", "ignored"), SqlIsNull)
    not_null = build_filter_condition(_COL, "IS NOT NULL", "ignored")
    assert isinstance(not_null, SqlBinary) and not_null.op == "IS NOT"


def test_in_with_scalar_wraps_into_one_element_list():
    condition = build_filter_condition(_COL, "IN", "Philadelphia")
    assert isinstance(condition, SqlIn)
    assert [v.value for v in condition.values] == ["Philadelphia"]
    assert condition.negated is False


def test_not_in_with_list_is_negated_sql_in():
    condition = build_filter_condition(_COL, "NOT IN", ["a", "b"])
    assert isinstance(condition, SqlIn)
    assert condition.negated is True
    assert [v.value for v in condition.values] == ["a", "b"]


def test_empty_in_and_not_in_lists_compile_to_constants():
    assert build_filter_condition(_COL, "IN", []) == SqlLiteral(False)
    assert build_filter_condition(_COL, "NOT IN", []) == SqlLiteral(True)


def test_in_with_null_value_is_rejected_with_recovery_hint():
    with pytest.raises(SemanticLayerError) as exc:
        build_filter_condition(_COL, "IN", None)
    assert exc.value.code == "INVALID_QUERY"
    hints = exc.value.details.get("recovery_hints", [])
    assert hints and hints[0]["code"] == "USE_LIST_VALUE_OR_NULL_TEST"


# ---------------------------------------------------------------------
# Normalize-time checks (``ast._filter_from_payload``).
# ---------------------------------------------------------------------


def _query_with_where(where_item: dict) -> dict:
    return {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}],
        "where": [where_item],
    }


def test_normalize_wraps_scalar_in_value_into_list():
    normalized = normalize_query(
        _query_with_where(
            {"field": "dimension.jaffle_store_name", "op": "IN", "value": "Philadelphia"}
        )
    )
    assert normalized.where[0].value == ["Philadelphia"]


def test_normalize_wraps_scalar_not_in_value_into_list():
    normalized = normalize_query(
        _query_with_where({"field": "dimension.jaffle_store_name", "op": "NOT IN", "value": 7})
    )
    assert normalized.where[0].value == [7]


def test_normalize_rejects_null_value_for_in_with_recovery_hint():
    with pytest.raises(SemanticLayerError) as exc:
        normalize_query(
            _query_with_where({"field": "dimension.jaffle_store_name", "op": "IN", "value": None})
        )
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == "where[0].value"
    hints = exc.value.details.get("recovery_hints", [])
    assert hints and "IS NULL" in hints[0]["message"]


# ---------------------------------------------------------------------
# Compile-level checks — rendered SQL through the full lowering stack.
# ---------------------------------------------------------------------


def _compiled_sql(config, where_item: dict) -> str:
    query = {
        "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}],
        "group_by": ["dimension.jaffle_store_name"],
        "where": [where_item],
    }
    return compile_query(config, Registry(config), query)["sql"]


def test_not_equals_null_renders_is_not_null(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _compiled_sql(
        config, {"field": "dimension.jaffle_store_name", "op": "!=", "value": None}
    )
    assert "store_name IS NOT NULL" in rendered
    assert "!= NULL" not in rendered


def test_greater_than_null_is_rejected_not_silently_lowered(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    with pytest.raises(SemanticLayerError) as exc:
        _compiled_sql(config, {"field": "dimension.jaffle_store_name", "op": ">", "value": None})
    assert exc.value.code == "INVALID_QUERY"


def test_in_with_scalar_string_does_not_character_split(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _compiled_sql(
        config, {"field": "dimension.jaffle_store_name", "op": "IN", "value": "Philadelphia"}
    )
    assert "IN ('Philadelphia')" in rendered
    assert "'P', 'h'" not in rendered


def test_schema_advertised_null_test_ops_compile(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _compiled_sql(config, {"field": "dimension.jaffle_store_name", "op": "IS NULL"})
    assert "store_name IS NULL" in rendered
    rendered = _compiled_sql(config, {"field": "dimension.jaffle_store_name", "op": "IS NOT NULL"})
    assert "store_name IS NOT NULL" in rendered


def test_schema_advertised_not_in_op_compiles(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _compiled_sql(
        config,
        {"field": "dimension.jaffle_store_name", "op": "NOT IN", "value": ["Philadelphia"]},
    )
    assert "NOT IN ('Philadelphia')" in rendered


def test_empty_in_list_compiles_to_constant_false(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _compiled_sql(
        config, {"field": "dimension.jaffle_store_name", "op": "IN", "value": []}
    )
    assert "FALSE" in rendered
    rendered = _compiled_sql(
        config, {"field": "dimension.jaffle_store_name", "op": "NOT IN", "value": []}
    )
    assert "TRUE" in rendered


# ---------------------------------------------------------------------
# Runtime-level check — validate() accepts every schema-advertised op.
# ---------------------------------------------------------------------


def test_validate_accepts_every_schema_advertised_where_op(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    cases = [
        {"op": "=", "value": "Philadelphia"},
        {"op": "!=", "value": "Philadelphia"},
        {"op": "!=", "value": None},
        {"op": "IN", "value": ["Philadelphia"]},
        {"op": "NOT IN", "value": ["Philadelphia"]},
        {"op": "LIKE", "value": "Phil%"},
        {"op": "NOT LIKE", "value": "Phil%"},
        {"op": "IS NULL", "value": None},
        {"op": "IS NOT NULL", "value": None},
    ]
    try:
        for case in cases:
            report = runtime.validate(
                {
                    "version": 1,
                    "select": [
                        {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}
                    ],
                    "where": [{"field": "dimension.jaffle_store_name", **case}],
                }
            )
            assert report["ok"] is True, (
                f"schema-advertised op {case['op']!r} failed validate: {report.get('errors')}"
            )
    finally:
        runtime.close()


# ---------------------------------------------------------------------
# A list value needs IN — a single-value op must not stringify it.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("op", ["=", "!=", "<", ">=", "LIKE"])
@pytest.mark.parametrize("value", [["Philadelphia", "Brooklyn"], ("Philadelphia",)])
def test_list_value_with_single_value_op_is_rejected(op, value):
    with pytest.raises(SemanticLayerError) as exc:
        build_filter_condition(_COL, op, value)
    assert exc.value.code == "INVALID_QUERY"
    hints = exc.value.details.get("recovery_hints", [])
    assert hints and hints[0]["code"] == "USE_IN_FOR_LIST_VALUE"
    assert "'IN'" in hints[0]["message"]


def _validate(runtime, **clauses) -> dict:
    return runtime.validate(
        {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "group_by": ["dimension.jaffle_store_name"],
            **clauses,
        }
    )


def test_validate_rejects_equals_with_a_list_instead_of_matching_nothing(runtime_factory):
    # Used to validate and compile to store_name = '[''Philadelphia'', ''Brooklyn'']',
    # which returned zero rows.
    runtime = runtime_factory("jaffle_shop")
    stores = ["Philadelphia", "Brooklyn"]
    try:
        report = _validate(
            runtime, where=[{"field": "dimension.jaffle_store_name", "op": "=", "value": stores}]
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_QUERY"
        assert [hint["code"] for hint in report["recovery_hints"]] == ["USE_IN_FOR_LIST_VALUE"]
        assert report["errors"][0]["recovery_hints"] == report["recovery_hints"]
        report = _validate(
            runtime, where=[{"field": "dimension.jaffle_store_name", "op": "in", "value": stores}]
        )
        assert report["ok"] is True
    finally:
        runtime.close()


def test_list_on_a_numeric_dimension_gets_the_in_hint_not_a_type_error(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    field = "dimension.jaffle_order_customer_order_number"
    try:
        report = _validate(runtime, where=[{"field": field, "op": "=", "value": [1, 2]}])
        assert report["ok"] is False
        error = report["errors"][0]
        assert error["code"] == "INVALID_QUERY"
        hints = error["recovery_hints"]
        assert [hint["code"] for hint in hints] == ["USE_IN_FOR_LIST_VALUE"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dimension.jaffle_store_name", [1, 2]),
        ("dimension.jaffle_order_customer_order_number", ["bad", 2]),
    ],
)
def test_wrong_type_list_still_gets_scalar_op_shape_hint_on_public_paths(
    runtime_factory, field, value
):
    runtime = runtime_factory("jaffle_shop")
    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "group_by": ["dimension.jaffle_store_name"],
        "where": [{"field": field, "op": "=", "value": value}],
    }
    try:
        report = runtime.validate(query)
        assert report["ok"] is False
        issue = report["errors"][0]
        assert issue["code"] == "INVALID_QUERY"
        assert [hint["code"] for hint in issue["recovery_hints"]] == ["USE_IN_FOR_LIST_VALUE"]
        assert report["recovery_hints"] == issue["recovery_hints"]
        for method in (runtime.compile, runtime.query):
            with pytest.raises(SemanticLayerError) as exc:
                method(query)
            assert exc.value.code == "INVALID_QUERY"
            assert [hint["code"] for hint in exc.value.details["recovery_hints"]] == [
                "USE_IN_FOR_LIST_VALUE"
            ]
    finally:
        runtime.close()


def test_numeric_membership_lists_and_scalar_type_diagnostics_stay_intact(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    field = "dimension.jaffle_order_customer_order_number"
    try:
        for op in ("IN", "NOT IN"):
            query = {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "where": [{"field": field, "op": op, "value": [1, 2]}],
            }
            assert runtime.validate(query)["ok"]
            assert runtime.compile(query)["ok"]
            assert runtime.query(query)["ok"]
        for op, value in (("=", "bad"), ("IN", ["bad", 2])):
            report = _validate(runtime, where=[{"field": field, "op": op, "value": value}])
            assert report["ok"] is False
            issue = report["errors"][0]
            assert issue["code"] == "INVALID_QUERY"
            assert "expects a numeric value" in issue["message"]
            assert [hint["kind"] for hint in issue["recovery_hints"]] == ["fix_filter_value_type"]
        string_report = _validate(
            runtime,
            where=[{"field": "dimension.jaffle_store_name", "op": "=", "value": 1}],
        )
        assert string_report["ok"] is False
        assert "expects a string value" in string_report["errors"][0]["message"]
        hints = string_report["errors"][0]["recovery_hints"]
        assert [hint["kind"] for hint in hints] == ["fix_filter_value_type"]
        assert "on text; in YAML, quote text" in hints[0]["message"]
    finally:
        runtime.close()


@pytest.mark.parametrize("op", ["IS NULL", "IS NOT NULL"])
def test_null_test_ignores_list_value_on_public_paths(runtime_factory, op):
    runtime = runtime_factory("jaffle_shop")
    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "group_by": ["dimension.jaffle_store_name"],
        "where": [{"field": "dimension.jaffle_store_name", "op": op, "value": [1, 2]}],
    }
    without_value = {
        **query,
        "where": [{"field": "dimension.jaffle_store_name", "op": op}],
    }
    try:
        assert runtime.validate(query)["ok"] is True
        assert runtime.compile(query)["sql_plan"] == runtime.compile(without_value)["sql_plan"]
        rows = runtime.query(query)["rows"]
        baseline_rows = runtime.query(without_value)["rows"]
        assert sorted(rows, key=lambda row: str(row["dimension.jaffle_store_name"])) == sorted(
            baseline_rows, key=lambda row: str(row["dimension.jaffle_store_name"])
        )
    finally:
        runtime.close()


def test_validate_rejects_a_list_in_a_metric_filter_comparison(runtime_factory):
    # Used to validate, then fail in the warehouse on the stringified list.
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _validate(
            runtime,
            metric_filters=[
                {
                    "expression": {"measure": "measure.jaffle.order_count"},
                    "op": "=",
                    "value": [1, 2],
                }
            ],
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_QUERY"
        assert [hint["code"] for hint in report["recovery_hints"]] == ["USE_IN_FOR_LIST_VALUE"]
        assert report["errors"][0]["recovery_hints"] == report["recovery_hints"]
    finally:
        runtime.close()
