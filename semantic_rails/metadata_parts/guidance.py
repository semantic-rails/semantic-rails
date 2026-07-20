"""Recommended-companion and preferred-aggregation guidance for inspect cards.

When ``inspect``/``catalog``/``discover`` returns a measure or metric-recipe
card, it splats one or both of these blocks into the response so agents see
"this is what the package recommends you use this measure/metric with":

* ``_aggregation_guidance(measure)`` — the default/allowed/suggested/
  discouraged aggregation policy declared by the measure itself.
* ``_metric_guidance(config, recipe)`` — same shape, but rolled up from the
  recipe's underlying measure (or pinned, when the recipe's expression is a
  fixed ``AggregateExpr``).
* ``_metric_preferences(config, recipe)`` — preferred dimensions and filter
  operators inherited from the recipe's underlying measure(s).

``_metric_measure_ids`` is the expression-traversal primitive both
``_metric_guidance`` and ``_metric_preferences`` rely on to find the
measures underneath a recipe — it lives here because guidance is its only
caller.
"""

from __future__ import annotations

from typing import Any

from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ComparisonExpr,
    ConversionExpr,
    CumulativeExpr,
    DistributionExpr,
    EntityValueExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    PeriodToDateExpr,
    PriorPeriodExpr,
    RatioExpr,
    RollingExpr,
    ScopedAggregateExpr,
    SemanticExpr,
)
from ..schema import PackageConfig


def _measures_by_id(config: PackageConfig) -> dict[str, Any]:
    return {row.id: row for row in config.measures}


def _dimensions_by_id(config: PackageConfig) -> dict[str, Any]:
    return {row.id: row for row in config.dimensions}


def _metric_recipes_by_id(config: PackageConfig) -> dict[str, Any]:
    return {row.id: row for row in config.metric_recipes}


def _aggregation_guidance(measure: Any) -> dict[str, Any]:
    discouraged = [
        {"aggregation": agg, "reason": "not valid for this measure class"}
        for agg in list(measure.invalid_aggregations)
    ]
    return {
        "default_aggregation": measure.default_aggregation,
        "allowed_aggregations": list(measure.allowed_aggregations),
        "suggested_aggregations": list(
            measure.suggested_aggregations or [measure.default_aggregation]
        ),
        "discouraged_aggregations": discouraged,
    }


def _metric_measure_ids(
    config: PackageConfig, expr: SemanticExpr, *, seen_metric_ids: set[str] | None = None
) -> list[str]:
    seen_metric_ids = set(seen_metric_ids or set())
    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        return [expr.measure]
    if isinstance(expr, MetricRecipeRefExpr):
        if expr.metric_recipe in seen_metric_ids:
            return []
        recipe = _metric_recipes_by_id(config).get(expr.metric_recipe)
        if recipe is None:
            return []
        return _metric_measure_ids(
            config, recipe.expression, seen_metric_ids=seen_metric_ids | {expr.metric_recipe}
        )
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return [
            *_metric_measure_ids(config, expr.left, seen_metric_ids=seen_metric_ids),
            *_metric_measure_ids(config, expr.right, seen_metric_ids=seen_metric_ids),
        ]
    if isinstance(expr, RatioExpr):
        return [
            *_metric_measure_ids(config, expr.numerator, seen_metric_ids=seen_metric_ids),
            *_metric_measure_ids(config, expr.denominator, seen_metric_ids=seen_metric_ids),
        ]
    if isinstance(expr, EntityValueExpr):
        return _metric_measure_ids(config, expr.input, seen_metric_ids=seen_metric_ids)
    if isinstance(expr, DistributionExpr):
        return _metric_measure_ids(config, expr.over, seen_metric_ids=seen_metric_ids)
    if isinstance(expr, BooleanExpr):
        out: list[str] = []
        for arg in expr.args:
            out.extend(_metric_measure_ids(config, arg, seen_metric_ids=seen_metric_ids))
        return out
    if isinstance(expr, CallExpr):
        out = []
        for arg in expr.args:
            out.extend(_metric_measure_ids(config, arg, seen_metric_ids=seen_metric_ids))
        return out
    if isinstance(expr, CaseExpr):
        out = []
        for item in expr.whens:
            out.extend(_metric_measure_ids(config, item.when, seen_metric_ids=seen_metric_ids))
            out.extend(_metric_measure_ids(config, item.then, seen_metric_ids=seen_metric_ids))
        if expr.else_expr is not None:
            out.extend(_metric_measure_ids(config, expr.else_expr, seen_metric_ids=seen_metric_ids))
        return out
    if isinstance(expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr)):
        return _metric_measure_ids(config, expr.input, seen_metric_ids=seen_metric_ids)
    if isinstance(expr, MetricPredicateExpr):
        return _metric_measure_ids(config, expr.input, seen_metric_ids=seen_metric_ids)
    if isinstance(expr, ConversionExpr):
        return [
            *_metric_measure_ids(config, expr.base, seen_metric_ids=seen_metric_ids),
            *_metric_measure_ids(config, expr.converted, seen_metric_ids=seen_metric_ids),
        ]
    return []


def _metric_guidance(config: PackageConfig, recipe: Any) -> dict[str, Any]:
    measure_ids = list(dict.fromkeys(_metric_measure_ids(config, recipe.expression)))
    if not measure_ids:
        return {
            "default_aggregation": "",
            "allowed_aggregations": [],
            "suggested_aggregations": [],
            "discouraged_aggregations": [],
        }
    primary_measure = _measures_by_id(config)[measure_ids[0]]
    if isinstance(recipe.expression, AggregateExpr):
        aggregation = recipe.expression.aggregation or primary_measure.default_aggregation
        return {
            "default_aggregation": aggregation,
            "allowed_aggregations": [aggregation],
            "suggested_aggregations": [aggregation],
            "discouraged_aggregations": [
                {"aggregation": agg, "reason": "metric recipe fixes aggregation semantics"}
                for agg in list(primary_measure.allowed_aggregations)
                if agg != aggregation
            ],
        }
    return _aggregation_guidance(primary_measure)


def _metric_preferences(config: PackageConfig, recipe: Any) -> dict[str, Any]:
    measure_ids = list(dict.fromkeys(_metric_measure_ids(config, recipe.expression)))
    if not measure_ids:
        return {"related_measures": []}
    return {"related_measures": measure_ids}
