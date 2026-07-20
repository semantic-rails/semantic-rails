"""Regression tests for boolean ``not`` in expression parsing and the
post-aggregation (metric_filters) lowering path.

Bug: ``_compile_post_expr`` folded ``BooleanExpr`` strictly as a binary
chain (``current = rendered[0]; for item in rendered[1:]: ...``), so a
``{kind: 'boolean', op: 'not', args: [<cmp>]}`` expression — explicitly
permitted by the query IR schema — silently returned the UN-NEGATED arg.
``parse_semantic_expression`` also accepted any ``op`` string, deferring
unsupported ops to an opaque render-time failure.
"""

from __future__ import annotations

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.post_aggregation import _compile_post_expr
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    BooleanExpr,
    ComparisonExpr,
    LiteralExpr,
    parse_semantic_expression,
)
from semantic_rails.registry import Registry
from semantic_rails.sql_ast import SqlBinary, SqlLiteral


def _comparison_dict(threshold: int = 10) -> dict:
    return {
        "kind": "comparison",
        "op": ">",
        "left": {"measure": "measure.jaffle.order_count"},
        "right": {"kind": "literal", "value": threshold},
    }


def _not_query(threshold: int = 10) -> dict:
    return {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "group_by": ["dimension.jaffle_store_name"],
        "metric_filters": [
            {
                "expression": {
                    "kind": "boolean",
                    "op": "not",
                    "args": [_comparison_dict(threshold)],
                },
                "op": "=",
                "value": True,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Parse-time validation (expressions.py)
# ---------------------------------------------------------------------------


def test_parse_accepts_not_and_normalizes_case():
    for raw_op in ("not", "NOT", "Not"):
        parsed = parse_semantic_expression(
            {"kind": "boolean", "op": raw_op, "args": [_comparison_dict()]}, context="query"
        )
        assert isinstance(parsed, BooleanExpr)
        assert parsed.op == "not"
        assert len(parsed.args) == 1


def test_parse_normalizes_and_or_case():
    for raw_op, expected in (("AND", "and"), ("Or", "or")):
        parsed = parse_semantic_expression(
            {
                "kind": "boolean",
                "op": raw_op,
                "args": [_comparison_dict(1), _comparison_dict(2)],
            },
            context="query",
        )
        assert isinstance(parsed, BooleanExpr)
        assert parsed.op == expected


def test_parse_rejects_unsupported_boolean_op():
    with pytest.raises(SemanticLayerError) as exc_info:
        parse_semantic_expression(
            {"kind": "boolean", "op": "xor", "args": [_comparison_dict(), _comparison_dict()]},
            context="query",
        )
    err = exc_info.value
    assert err.code == "INVALID_EXPRESSION_AST"
    assert err.details["allowed"] == ["and", "or", "not"]
    assert err.details["received_value"] == "xor"
    assert err.details["recovery_hints"]
    assert err.details["recovery_hints"][0]["code"] == "USE_SUPPORTED_BOOLEAN_OP"


def test_parse_rejects_not_with_multiple_args():
    with pytest.raises(SemanticLayerError) as exc_info:
        parse_semantic_expression(
            {
                "kind": "boolean",
                "op": "not",
                "args": [_comparison_dict(1), _comparison_dict(2)],
            },
            context="query",
        )
    err = exc_info.value
    assert err.code == "INVALID_EXPRESSION_AST"
    assert err.details["received_arg_count"] == 2
    assert err.details["recovery_hints"][0]["code"] == "WRAP_NOT_ARGS"


def test_parse_still_rejects_empty_args():
    with pytest.raises(SemanticLayerError) as exc_info:
        parse_semantic_expression({"kind": "boolean", "op": "and", "args": []}, context="query")
    assert exc_info.value.code == "INVALID_EXPRESSION_AST"


# ---------------------------------------------------------------------------
# Post-aggregation lowering (compiler_parts/post_aggregation.py)
# ---------------------------------------------------------------------------


def _not_expr(inner: ComparisonExpr | None = None) -> BooleanExpr:
    inner = inner or ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(2))
    return BooleanExpr(op="not", args=[inner])


def test_compile_post_expr_not_emits_negation(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    compiled = _compile_post_expr(_not_expr(), config)
    # Negation is composed as ``FALSE = (arg)`` (same three-valued truth
    # table as ``NOT arg``); the un-negated arg must not leak through.
    assert isinstance(compiled, SqlBinary)
    assert compiled.op == "="
    assert compiled.left == SqlLiteral(False)
    assert isinstance(compiled.right, SqlBinary)
    assert compiled.right.op == ">"


def test_compile_post_expr_not_requires_exactly_one_arg(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    two_args = BooleanExpr(
        op="not",
        args=[
            ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(2)),
            ComparisonExpr(op="<", left=LiteralExpr(3), right=LiteralExpr(4)),
        ],
    )
    with pytest.raises(SemanticLayerError) as exc_info:
        _compile_post_expr(two_args, config)
    err = exc_info.value
    assert err.code == "INVALID_EXPRESSION_AST"
    assert err.details["received_arg_count"] == 2


def test_compile_post_expr_rejects_unsupported_boolean_op(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    bogus = BooleanExpr(
        op="xor",
        args=[
            ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(2)),
            ComparisonExpr(op="<", left=LiteralExpr(3), right=LiteralExpr(4)),
        ],
    )
    with pytest.raises(SemanticLayerError) as exc_info:
        _compile_post_expr(bogus, config)
    err = exc_info.value
    assert err.code == "INVALID_EXPRESSION_AST"
    assert err.details["allowed"] == ["and", "or", "not"]


def test_compile_post_expr_rejects_empty_boolean_args(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    with pytest.raises(SemanticLayerError) as exc_info:
        _compile_post_expr(BooleanExpr(op="and", args=[]), config)
    assert exc_info.value.code == "INVALID_EXPRESSION_AST"


def test_compile_post_expr_and_fold_unchanged(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    fold = BooleanExpr(
        op="and",
        args=[
            ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(2)),
            ComparisonExpr(op="<", left=LiteralExpr(3), right=LiteralExpr(4)),
        ],
    )
    compiled = _compile_post_expr(fold, config)
    assert isinstance(compiled, SqlBinary)
    assert compiled.op == "AND"


def test_metric_filter_not_renders_negated_sql(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    compiled = compile_query(config, Registry(config), _not_query(threshold=10))
    rendered = compiled["sql"]
    # The arg must be negated AND parenthesized — chained comparisons
    # (``x > 10 = FALSE``) are non-associative in Snowflake/Postgres.
    assert "FALSE = (base.m1 > 10)" in rendered


def test_metric_filter_not_uppercase_op_renders_negated_sql(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = _not_query(threshold=10)
    query["metric_filters"][0]["expression"]["op"] = "NOT"
    compiled = compile_query(config, Registry(config), query)
    assert "FALSE = (base.m1 > 10)" in compiled["sql"]


# ---------------------------------------------------------------------------
# End-to-end semantics on real DuckDB data
# ---------------------------------------------------------------------------


def test_metric_filter_not_executes_as_complement(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        base = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "group_by": ["dimension.jaffle_store_name"],
        }
        unfiltered = runtime.query(base)
        counts = {row["dimension.jaffle_store_name"]: row["orders"] for row in unfiltered["rows"]}
        assert len(counts) > 1
        # Split the stores: ``> threshold`` keeps the strictly-larger ones,
        # ``not (> threshold)`` must keep exactly the rest.
        threshold = min(counts.values())

        def _query(negate: bool) -> dict:
            comparison = _comparison_dict(threshold)
            expression = (
                {"kind": "boolean", "op": "not", "args": [comparison]} if negate else comparison
            )
            payload = dict(base)
            payload["metric_filters"] = [{"expression": expression, "op": "=", "value": True}]
            return payload

        negated = runtime.query(_query(negate=True))
        positive = runtime.query(_query(negate=False))

        negated_stores = {row["dimension.jaffle_store_name"] for row in negated["rows"]}
        positive_stores = {row["dimension.jaffle_store_name"] for row in positive["rows"]}

        assert negated_stores == {store for store, c in counts.items() if not c > threshold}
        assert positive_stores == {store for store, c in counts.items() if c > threshold}
        assert negated_stores.isdisjoint(positive_stores)
        assert negated_stores | positive_stores == set(counts)
    finally:
        runtime.close()


class TestConfigExprNotLowering:
    """The same dropped-``not`` fold existed in the pre-aggregation
    config-expression lowering (``bind._config_expr_to_sql``) and the
    relation-pipeline expression lowering
    (``relation_pipelines._semantic_expr_to_sql``)."""

    def test_bind_config_expr_not_is_negated(self, package_config_factory) -> None:
        from semantic_rails.compiler_parts.bind import _config_expr_to_sql
        from semantic_rails.renderer import render_expr

        config, _ = package_config_factory("jaffle_shop")
        measure = config.measures[0]
        comparison = ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(5))
        rendered = render_expr(
            _config_expr_to_sql(BooleanExpr(op="not", args=[comparison]), measure, config)
        )
        assert rendered == "FALSE = (1 > 5)"

    def test_bind_config_expr_not_requires_one_arg(self, package_config_factory) -> None:
        from semantic_rails.compiler_parts.bind import _config_expr_to_sql

        config, _ = package_config_factory("jaffle_shop")
        measure = config.measures[0]
        comparison = ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(5))
        with pytest.raises(SemanticLayerError) as exc:
            _config_expr_to_sql(
                BooleanExpr(op="not", args=[comparison, comparison]), measure, config
            )
        assert exc.value.code == "INVALID_EXPRESSION_AST"

    def test_relation_pipeline_expr_not_is_negated(self) -> None:
        from semantic_rails.relation_pipelines import _semantic_expr_to_sql
        from semantic_rails.renderer import render_expr

        comparison = ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(5))
        rendered = render_expr(_semantic_expr_to_sql(BooleanExpr(op="not", args=[comparison])))
        assert rendered == "FALSE = (1 > 5)"

    def test_relation_pipeline_expr_not_requires_one_arg(self) -> None:
        from semantic_rails.relation_pipelines import _semantic_expr_to_sql

        comparison = ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(5))
        with pytest.raises(SemanticLayerError) as exc:
            _semantic_expr_to_sql(BooleanExpr(op="not", args=[comparison, comparison]))
        assert exc.value.code == "INVALID_EXPRESSION_AST"
