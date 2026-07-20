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

The shared mapping lives in ``sql_ast.build_filter_condition`` so every
filter lowering site (query ``where[]``, measure-bound filters, metric
filter envelopes, predicate sources) behaves identically.
"""

from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlIdentifier,
    SqlIn,
    SqlIsNull,
    SqlLiteral,
    build_filter_condition,
)

# ---------------------------------------------------------------------
# Unit checks on the consolidated helper.
# ---------------------------------------------------------------------

_COL = SqlIdentifier(parts=["t", "c"])


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
