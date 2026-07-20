from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.config import load_package_config, resolve_repo_path
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
)


def test_parse_config_expression_supports_column_math():
    expr = parse_config_expression("(order_total_cents / 100.0)")

    assert isinstance(expr, ArithmeticExpr)
    assert isinstance(expr.left, ColumnRefExpr)
    assert expr.left.column == "order_total_cents"
    assert isinstance(expr.right, LiteralExpr)
    assert expr.right.value == 100.0


def test_parse_config_expression_supports_in_lists():
    expr = parse_config_expression("product in ['email', 'sms']")

    assert isinstance(expr, InExpr)
    assert isinstance(expr.expr, ColumnRefExpr)
    assert expr.expr.column == "product"
    assert [value.value for value in expr.values if isinstance(value, LiteralExpr)] == [
        "email",
        "sms",
    ]
    assert expr.negated is False

    negated = parse_config_expression("product not in ['push']")
    assert isinstance(negated, InExpr)
    assert negated.negated is True


def test_parse_semantic_expression_supports_date_add_and_in_round_trip():
    expr = parse_semantic_expression(
        {
            "kind": "date_add",
            "unit": "day",
            "value": {"kind": "column", "column": "maturity_days"},
            "date": {"kind": "column", "column": "created_at"},
        },
        context="config",
    )
    assert isinstance(expr, DateAddExpr)
    assert expr_to_dict(expr)["kind"] == "date_add"

    in_expr = parse_semantic_expression(
        {
            "kind": "in",
            "expr": {"kind": "column", "column": "product"},
            "values": ["email", "sms"],
            "negated": True,
        },
        context="config",
    )
    assert isinstance(in_expr, InExpr)
    assert expr_to_dict(in_expr)["negated"] is True

    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {"kind": "in", "expr": {"kind": "column", "column": "product"}, "values": []},
            context="config",
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"


def test_runtime_query_normalization_uses_typed_expressions():
    query = normalize_query(
        {
            "select": [
                {
                    "expression": {
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


def test_package_loader_parses_measure_exprs_into_expression_ast():
    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    revenue = next(row for row in config.measures if row.id == "measure.jaffle.revenue_usd")

    assert isinstance(revenue.expr, ArithmeticExpr)
    assert isinstance(revenue.expr.left, ColumnRefExpr)
    assert revenue.expr.left.column == "order_total_cents"


def test_parse_semantic_expression_preserves_aggregate_parameters():
    expr = parse_semantic_expression(
        {
            "kind": "aggregate",
            "measure": "measure.jaffle.item_revenue_usd",
            "aggregation": "percentile",
            "parameters": {"p": 0.95},
        },
        context="query",
    )

    assert isinstance(expr, AggregateExpr)
    assert expr.parameters == {"p": 0.95}


def test_parse_metric_predicate_expression_round_trips():
    expr = parse_semantic_expression(
        {
            "kind": "metric_predicate",
            "entity": "entity.jaffle_customer",
            "input": {"metric": "metric.sales.lifetime_order_count"},
            "op": ">",
            "value": 1,
        },
        context="query",
    )

    assert isinstance(expr, MetricPredicateExpr)
    assert expr.entity == "entity.jaffle_customer"
    assert expr.op == ">"
    assert expr.scope_mode == "contextual"
    assert expr_to_dict(expr)["kind"] == "metric_predicate"
    assert expr_to_dict(expr)["scope_mode"] == "contextual"


def test_package_authored_metric_predicate_requires_scope_mode_and_preserves_time_grain():
    with pytest.raises(SemanticLayerError) as missing_scope:
        parse_semantic_expression(
            {
                "kind": "metric_predicate",
                "entity": "entity.jaffle_customer",
                "input": {"metric": "metric.sales.orders"},
                "op": ">",
                "value": 10,
            },
            context="config",
        )
    assert missing_scope.value.code == "INVALID_METRIC_PREDICATE"

    expr = parse_semantic_expression(
        {
            "kind": "metric_predicate",
            "entity": "entity.jaffle_customer",
            "scope_mode": "contextual",
            "time_grain": "month",
            "input": {"metric": "metric.sales.orders"},
            "op": ">",
            "value": 10,
        },
        context="config",
    )

    assert isinstance(expr, MetricPredicateExpr)
    assert expr.scope_mode == "contextual"
    assert expr.time_grain == "month"


def test_entity_only_metric_predicate_cannot_declare_time_grain():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "metric_predicate",
                "entity": "entity.jaffle_customer",
                "scope_mode": "entity_only",
                "time_grain": "month",
                "input": {"metric": "metric.sales.lifetime_order_count"},
                "op": ">",
                "value": 1,
            },
            context="config",
        )
    assert exc.value.code == "INVALID_METRIC_PREDICATE"


def test_metric_predicate_validates_time_alignment():
    same_period = parse_semantic_expression(
        {
            "kind": "metric_predicate",
            "entity": "entity.jaffle_customer",
            "scope_mode": "contextual",
            "time_alignment": "same_query_period",
            "input": {"metric": "metric.sales.orders"},
            "op": ">",
            "value": 1,
        },
        context="query",
    )
    assert isinstance(same_period, MetricPredicateExpr)
    assert same_period.time_alignment == "same_query_period"

    with pytest.raises(SemanticLayerError) as invalid_alignment:
        parse_semantic_expression(
            {
                "kind": "metric_predicate",
                "entity": "entity.jaffle_customer",
                "scope_mode": "contextual",
                "time_alignment": "next_query_period",
                "input": {"metric": "metric.sales.orders"},
                "op": ">",
                "value": 1,
            },
            context="query",
        )
    assert invalid_alignment.value.code == "INVALID_METRIC_PREDICATE"

    with pytest.raises(SemanticLayerError) as contextual_alignment:
        parse_semantic_expression(
            {
                "kind": "metric_predicate",
                "entity": "entity.jaffle_customer",
                "scope_mode": "contextual",
                "time_alignment": "query_window",
                "input": {"metric": "metric.sales.orders"},
                "op": ">",
                "value": 1,
            },
            context="query",
        )
    assert contextual_alignment.value.code == "INVALID_METRIC_PREDICATE"


def test_parse_enriched_conversion_expression_round_trips():
    expr = parse_semantic_expression(
        {
            "kind": "conversion",
            "entity": "entity.jaffle_customer",
            "window": {"unit": "day", "value": 7},
            "matching_mode": "first_converted_after_base",
            "constant_properties": ["dimension.jaffle_store_name"],
            "base": {"metric": "metric.sales.session_starts"},
            "converted": {"metric": "metric.sales.orders"},
        },
        context="query",
    )

    assert isinstance(expr, ConversionExpr)
    assert expr.entity == "entity.jaffle_customer"
    assert expr.window_unit == "day"
    assert expr.window_value == 7
    assert expr.matching_mode == "first_converted_after_base"
    assert expr_to_dict(expr)["constant_properties"] == ["dimension.jaffle_store_name"]


def _conversion_payload(**overrides):
    base = {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 7},
        "matching_mode": "first_converted_after_base",
        "base": {"metric": "metric.sales.session_starts"},
        "converted": {"metric": "metric.sales.orders"},
    }
    base.update(overrides)
    return base


def test_conversion_without_entity_raises_conversion_entity_required():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(_conversion_payload(entity=""), context="query")
    assert exc.value.code == "CONVERSION_ENTITY_REQUIRED"


def test_conversion_with_missing_window_raises_conversion_window_required():
    # Missing unit + zero value → window required
    with pytest.raises(SemanticLayerError) as missing_unit:
        parse_semantic_expression(_conversion_payload(window={"value": 7}), context="query")
    assert missing_unit.value.code == "CONVERSION_WINDOW_REQUIRED"

    # Non-positive value → window required
    with pytest.raises(SemanticLayerError) as zero_value:
        parse_semantic_expression(
            _conversion_payload(window={"unit": "day", "value": 0}), context="query"
        )
    assert zero_value.value.code == "CONVERSION_WINDOW_REQUIRED"

    # Non-integer value → window required (TypeError/ValueError path)
    with pytest.raises(SemanticLayerError) as bad_value:
        parse_semantic_expression(
            _conversion_payload(window={"unit": "day", "value": "abc"}), context="query"
        )
    assert bad_value.value.code == "CONVERSION_WINDOW_REQUIRED"


def test_conversion_with_unsupported_matching_mode_raises_conversion_matching_mode_required():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            _conversion_payload(matching_mode="last_converted_before_base"),
            context="query",
        )
    assert exc.value.code == "CONVERSION_MATCHING_MODE_REQUIRED"

    # Empty matching_mode → also required
    with pytest.raises(SemanticLayerError) as empty:
        parse_semantic_expression(_conversion_payload(matching_mode=""), context="query")
    assert empty.value.code == "CONVERSION_MATCHING_MODE_REQUIRED"


def test_metric_predicate_without_entity_raises_predicate_entity_required():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "metric_predicate",
                "entity": "",
                "scope_mode": "contextual",
                "input": {"metric": "metric.sales.orders"},
                "op": ">",
                "value": 1,
            },
            context="query",
        )
    assert exc.value.code == "PREDICATE_ENTITY_REQUIRED"


def test_metric_predicate_without_input_raises_predicate_input_required():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "metric_predicate",
                "entity": "entity.jaffle_customer",
                "scope_mode": "contextual",
                "op": ">",
                "value": 1,
            },
            context="query",
        )
    assert exc.value.code == "PREDICATE_INPUT_REQUIRED"


def test_parse_between_desugars_to_and_of_comparisons():
    expr = parse_semantic_expression(
        {
            "kind": "between",
            "expr": {"kind": "column", "column": "order_total_cents"},
            "low": {"kind": "literal", "value": 100},
            "high": {"kind": "literal", "value": 500},
        },
        context="config",
    )

    assert isinstance(expr, BooleanExpr)
    assert expr.op == "and"
    assert len(expr.args) == 2

    lower, upper = expr.args
    assert isinstance(lower, ComparisonExpr)
    assert lower.op == ">="
    assert isinstance(lower.left, ColumnRefExpr)
    assert lower.left.column == "order_total_cents"
    assert isinstance(lower.right, LiteralExpr)
    assert lower.right.value == 100

    assert isinstance(upper, ComparisonExpr)
    assert upper.op == "<="
    assert isinstance(upper.right, LiteralExpr)
    assert upper.right.value == 500


def test_parse_not_between_desugars_to_or_of_inverted_comparisons():
    expr = parse_semantic_expression(
        {
            "kind": "not_between",
            "expr": {"kind": "column", "column": "order_total_cents"},
            "low": {"kind": "literal", "value": 100},
            "high": {"kind": "literal", "value": 500},
        },
        context="config",
    )

    assert isinstance(expr, BooleanExpr)
    assert expr.op == "or"
    assert len(expr.args) == 2

    lower, upper = expr.args
    assert isinstance(lower, ComparisonExpr) and lower.op == "<"
    assert isinstance(upper, ComparisonExpr) and upper.op == ">"


def test_parse_between_with_negated_flag_matches_not_between():
    negated_flag = parse_semantic_expression(
        {
            "kind": "between",
            "expr": {"kind": "column", "column": "x"},
            "low": {"kind": "literal", "value": 1},
            "high": {"kind": "literal", "value": 2},
            "negated": True,
        },
        context="config",
    )
    not_kind = parse_semantic_expression(
        {
            "kind": "not_between",
            "expr": {"kind": "column", "column": "x"},
            "low": {"kind": "literal", "value": 1},
            "high": {"kind": "literal", "value": 2},
        },
        context="config",
    )

    assert expr_to_dict(negated_flag) == expr_to_dict(not_kind)


def test_parse_between_accepts_arbitrary_subexpressions_for_bounds():
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

    assert isinstance(expr, BooleanExpr)
    lower, upper = expr.args
    assert isinstance(lower, ComparisonExpr)
    assert isinstance(lower.right, DateAddExpr)
    assert isinstance(upper.right, ColumnRefExpr)
    assert upper.right.column == "expected_at"


def test_parse_between_missing_expr_raises_invalid_expression_ast():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "between",
                "low": {"kind": "literal", "value": 1},
                "high": {"kind": "literal", "value": 2},
            },
            context="config",
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"


def test_parse_between_missing_high_raises_invalid_expression_ast():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "between",
                "expr": {"kind": "column", "column": "x"},
                "low": {"kind": "literal", "value": 1},
            },
            context="config",
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"


def test_parse_aggregate_if_count_omits_value():
    expr = parse_semantic_expression(
        {
            "kind": "aggregate_if",
            "aggregation": "count",
            "condition": {
                "kind": "comparison",
                "op": "=",
                "left": {"kind": "column", "column": "is_new_customer_order"},
                "right": {"kind": "literal", "value": True},
            },
        },
        context="query",
    )

    assert isinstance(expr, ConditionalAggregateExpr)
    assert expr.aggregation == "count"
    assert expr.value is None
    assert isinstance(expr.condition, ComparisonExpr)


def test_parse_aggregate_if_sum_requires_value():
    expr = parse_semantic_expression(
        {
            "kind": "aggregate_if",
            "aggregation": "sum",
            "condition": {"kind": "column", "column": "is_large_order"},
            "value": {"kind": "column", "column": "order_total_cents"},
        },
        context="query",
    )

    assert isinstance(expr, ConditionalAggregateExpr)
    assert expr.aggregation == "sum"
    assert isinstance(expr.value, ColumnRefExpr)
    assert expr.value.column == "order_total_cents"

    # Round-trip through expr_to_dict preserves the shape.
    payload = expr_to_dict(expr)
    assert payload["kind"] == "aggregate_if"
    assert payload["aggregation"] == "sum"
    assert payload["value"]["column"] == "order_total_cents"


def test_parse_aggregate_if_sum_without_value_raises():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "aggregate_if",
                "aggregation": "sum",
                "condition": {"kind": "column", "column": "x"},
            },
            context="query",
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert "value" in str(exc.value).lower()


def test_parse_aggregate_if_missing_aggregation_raises():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "aggregate_if",
                "condition": {"kind": "column", "column": "x"},
            },
            context="query",
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"


def test_parse_aggregate_if_missing_condition_raises():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {"kind": "aggregate_if", "aggregation": "count"},
            context="query",
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"


def test_parse_aggregate_if_rejects_unknown_keys():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "aggregate_if",
                "aggregation": "count",
                "condition": {"kind": "column", "column": "x"},
                "filter": {"all": []},  # not a supported key
            },
            context="query",
        )
    assert exc.value.code == "INVALID_EXPRESSION_KEY"


def test_parse_between_rejects_unknown_keys():
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {
                "kind": "between",
                "expr": {"kind": "column", "column": "x"},
                "low": {"kind": "literal", "value": 1},
                "high": {"kind": "literal", "value": 2},
                "inclusive": True,  # not a supported key
            },
            context="config",
        )
    assert exc.value.code == "INVALID_EXPRESSION_KEY"
