from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    ColumnRefExpr,
    ComparisonExpr,
    ConditionalAggregateExpr,
    ConversionExpr,
    DateAddExpr,
    InExpr,
    LiteralExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    expr_to_dict,
    parse_config_expression,
    parse_semantic_expression,
    validate_expression_calls,
)

X = {"kind": "column", "column": "x"}
ONE = {"kind": "literal", "value": 1}
TWO = {"kind": "literal", "value": 2}


def _predicate(**overrides):
    return {
        "kind": "metric_predicate",
        "entity": "entity.jaffle_customer",
        "scope_mode": "contextual",
        "input": {"metric": "metric.sales.orders"},
        "op": ">",
        "value": 1,
        **overrides,
    }


def _conversion(**overrides):
    return {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 7},
        "matching_mode": "first_converted_after_base",
        "base": {"metric": "metric.sales.session_starts"},
        "converted": {"metric": "metric.sales.orders"},
        **overrides,
    }


def test_parse_config_expression_strings():
    expr = parse_config_expression("(order_total_cents / 100.0)")
    assert isinstance(expr, ArithmeticExpr)
    assert isinstance(expr.left, ColumnRefExpr) and expr.left.column == "order_total_cents"
    assert isinstance(expr.right, LiteralExpr) and expr.right.value == 100.0

    expr = parse_config_expression("product in ['email', 'sms']")
    assert isinstance(expr, InExpr) and expr.negated is False
    assert isinstance(expr.expr, ColumnRefExpr) and expr.expr.column == "product"
    assert [value.value for value in expr.values if isinstance(value, LiteralExpr)] == [
        "email",
        "sms",
    ]
    negated = parse_config_expression("product not in ['push']")
    assert isinstance(negated, InExpr) and negated.negated is True

    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    revenue = next(row for row in config.measures if row.id == "measure.jaffle.revenue_usd")
    assert isinstance(revenue.expr, ArithmeticExpr)
    assert revenue.expr.left.column == "order_total_cents"


def test_parse_semantic_expression_round_trips():
    date_add = {
        "kind": "date_add",
        "unit": "day",
        "value": {"kind": "column", "column": "maturity_days"},
        "date": {"kind": "column", "column": "created_at"},
    }
    expr = parse_semantic_expression(date_add, context="config")
    assert isinstance(expr, DateAddExpr) and expr_to_dict(expr)["kind"] == "date_add"
    in_list = {"kind": "in", "expr": X, "values": ["email", "sms"], "negated": True}
    expr = parse_semantic_expression(in_list, context="config")
    assert isinstance(expr, InExpr) and expr_to_dict(expr)["negated"] is True

    percentile = {
        "kind": "aggregate",
        "measure": "measure.jaffle.item_revenue_usd",
        "aggregation": "percentile",
        "parameters": {"p": 0.95},
    }
    expr = parse_semantic_expression(percentile, context="query")
    assert isinstance(expr, AggregateExpr) and expr.parameters == {"p": 0.95}

    expr = parse_semantic_expression(
        _conversion(constant_properties=["dimension.jaffle_store_name"]), context="query"
    )
    assert isinstance(expr, ConversionExpr)
    assert (expr.entity, expr.window_unit, expr.window_value, expr.matching_mode) == (
        "entity.jaffle_customer",
        "day",
        7,
        "first_converted_after_base",
    )
    assert expr_to_dict(expr)["constant_properties"] == ["dimension.jaffle_store_name"]

    query = normalize_query(
        {
            "select": [
                {
                    "expression": {
                        "kind": "measure",
                        "measure": "measure.jaffle.order_count",
                        "aggregation": "count_distinct",
                    },
                    "as": "orders",
                }
            ],
        }
    )
    assert isinstance(query.select[0].expression, MeasureRefExpr)
    assert query.select[0].expression.measure == "measure.jaffle.order_count"


def test_metric_predicates_keep_scope_grain_and_alignment():
    raw = _predicate(input={"metric": "metric.sales.lifetime_order_count"})
    del raw["scope_mode"]
    expr = parse_semantic_expression(raw, context="query")
    assert isinstance(expr, MetricPredicateExpr)
    assert (expr.entity, expr.op, expr.scope_mode) == ("entity.jaffle_customer", ">", "contextual")
    assert expr_to_dict(expr)["kind"] == "metric_predicate"
    assert expr_to_dict(expr)["scope_mode"] == "contextual"

    expr = parse_semantic_expression(_predicate(time_grain="month"), context="config")
    assert (expr.scope_mode, expr.time_grain) == ("contextual", "month")
    expr = parse_semantic_expression(
        _predicate(time_alignment="same_query_period"), context="query"
    )
    assert expr.time_alignment == "same_query_period"


def test_between_desugars_to_comparisons():
    expr = parse_semantic_expression(
        {
            "kind": "between",
            "expr": {"kind": "column", "column": "order_total_cents"},
            "low": {"kind": "literal", "value": 100},
            "high": {"kind": "literal", "value": 500},
        },
        context="config",
    )
    assert isinstance(expr, BooleanExpr) and expr.op == "and"
    lower, upper = expr.args
    assert isinstance(lower, ComparisonExpr) and lower.op == ">="
    assert isinstance(lower.left, ColumnRefExpr) and lower.left.column == "order_total_cents"
    assert isinstance(lower.right, LiteralExpr) and lower.right.value == 100
    assert isinstance(upper, ComparisonExpr) and upper.op == "<="
    assert isinstance(upper.right, LiteralExpr) and upper.right.value == 500

    not_between = parse_semantic_expression(
        {"kind": "not_between", "expr": X, "low": ONE, "high": TWO}, context="config"
    )
    assert isinstance(not_between, BooleanExpr) and not_between.op == "or"
    assert [arg.op for arg in not_between.args] == ["<", ">"]
    negated_flag = parse_semantic_expression(
        {"kind": "between", "expr": X, "low": ONE, "high": TWO, "negated": True},
        context="config",
    )
    assert expr_to_dict(negated_flag) == expr_to_dict(not_between)

    # Bounds may be any expression.
    expr = parse_semantic_expression(
        {
            "kind": "between",
            "expr": {"kind": "column", "column": "shipped_at"},
            "low": {
                "kind": "date_add",
                "unit": "day",
                "value": {"kind": "literal", "value": -7},
                "date": {"kind": "column", "column": "ordered_at"},
            },
            "high": {"kind": "column", "column": "expected_at"},
        },
        context="config",
    )
    lower, upper = expr.args
    assert isinstance(lower.right, DateAddExpr)
    assert isinstance(upper.right, ColumnRefExpr) and upper.right.column == "expected_at"


def test_aggregate_if_value_is_optional_only_for_count():
    count = parse_semantic_expression(
        {
            "kind": "aggregate_if",
            "aggregation": "count",
            "condition": {"kind": "comparison", "op": "=", "left": X, "right": ONE},
        },
        context="query",
    )
    assert isinstance(count, ConditionalAggregateExpr)
    assert (count.aggregation, count.value) == ("count", None)
    assert isinstance(count.condition, ComparisonExpr)

    total = parse_semantic_expression(
        {
            "kind": "aggregate_if",
            "aggregation": "sum",
            "condition": X,
            "value": {"kind": "column", "column": "order_total_cents"},
        },
        context="query",
    )
    assert isinstance(total.value, ColumnRefExpr) and total.value.column == "order_total_cents"
    payload = expr_to_dict(total)
    assert (payload["kind"], payload["aggregation"]) == ("aggregate_if", "sum")
    assert payload["value"]["column"] == "order_total_cents"

    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {"kind": "aggregate_if", "aggregation": "sum", "condition": X}, context="query"
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert exc.value.details == {"aggregation": "sum", "expression_kind": "aggregate_if"}


@pytest.mark.parametrize(
    "raw,context,code",
    [
        ({"kind": "in", "expr": X, "values": []}, "config", "INVALID_EXPRESSION_AST"),
        (
            {key: value for key, value in _predicate().items() if key != "scope_mode"},
            "config",
            "INVALID_METRIC_PREDICATE",
        ),
        (
            _predicate(scope_mode="entity_only", time_grain="month"),
            "config",
            "INVALID_METRIC_PREDICATE",
        ),
        (_predicate(time_alignment="next_query_period"), "query", "INVALID_METRIC_PREDICATE"),
        (_predicate(time_alignment="query_window"), "query", "INVALID_METRIC_PREDICATE"),
        (_predicate(entity=""), "query", "PREDICATE_ENTITY_REQUIRED"),
        (
            {key: value for key, value in _predicate().items() if key != "input"},
            "query",
            "PREDICATE_INPUT_REQUIRED",
        ),
        (_conversion(entity=""), "query", "CONVERSION_ENTITY_REQUIRED"),
        (_conversion(window={"value": 7}), "query", "CONVERSION_WINDOW_REQUIRED"),
        (_conversion(window={"unit": "day", "value": 0}), "query", "CONVERSION_WINDOW_REQUIRED"),
        (
            _conversion(window={"unit": "day", "value": "abc"}),
            "query",
            "CONVERSION_WINDOW_REQUIRED",
        ),
        (
            _conversion(matching_mode="last_converted_before_base"),
            "query",
            "CONVERSION_MATCHING_MODE_REQUIRED",
        ),
        (_conversion(matching_mode=""), "query", "CONVERSION_MATCHING_MODE_REQUIRED"),
        ({"kind": "between", "low": ONE, "high": TWO}, "config", "INVALID_EXPRESSION_AST"),
        ({"kind": "between", "expr": X, "low": ONE}, "config", "INVALID_EXPRESSION_AST"),
        (
            {"kind": "between", "expr": X, "low": ONE, "high": TWO, "inclusive": True},
            "config",
            "INVALID_EXPRESSION_KEY",
        ),
        ({"kind": "aggregate_if", "condition": X}, "query", "INVALID_EXPRESSION_AST"),
        ({"kind": "aggregate_if", "aggregation": "count"}, "query", "INVALID_EXPRESSION_AST"),
        (
            {"kind": "aggregate_if", "aggregation": "count", "condition": X, "filter": {"all": []}},
            "query",
            "INVALID_EXPRESSION_KEY",
        ),
    ],
)
def test_malformed_expressions_are_refused(raw, context, code):
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(raw, context=context)
    assert exc.value.code == code


def test_coverage_cutoff_functions_are_engine_only():
    for name in ("NOW", "now", "PG_TYPEOF", "pg_typeof"):
        expression = parse_semantic_expression(
            {"kind": "call", "name": name, "args": []}, context="query"
        )
        with pytest.raises(SemanticLayerError) as caught:
            validate_expression_calls(expression, dialect_for_warehouse("duckdb"))
        assert caught.value.code == "INVALID_EXPRESSION_AST", name
