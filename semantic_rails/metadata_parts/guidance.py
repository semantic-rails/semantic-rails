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
    ColumnRefExpr,
    ComparisonExpr,
    ConversionExpr,
    DistributionExpr,
    EntityValueExpr,
    LiteralExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    RatioExpr,
    ScopedAggregateExpr,
    SemanticExpr,
)
from ..schema import PackageConfig


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


def _count_unit(measure: Any) -> dict[str, Any]:
    """What a count measure counts, read from its definition alone: rows of its model, or
    distinct values of a column across them. ``{}`` for a measure that adds up, averages or
    carries a value (a lookup), a stock, or one whose counted expression names no single column.

    A count of distinct keys and a count of rows differ wherever a key repeats (customers who
    ordered vs their orders), so the card names which one the measure is."""
    # A stock reads one snapshot per series, whatever its aggregation: never every row it holds.
    if measure.lookup_from or measure.measure_class == "semi_additive":
        return {}
    aggregation = str(measure.default_aggregation or "").lower()
    expr, conditional = measure.expr, False
    if isinstance(expr, CaseExpr) and expr.whens:
        first = expr.whens[0].then
        other = expr.else_expr
        # Rows where its condition holds: every branch yields one value and the rest read NULL
        # (or, for a sum of 1, 0); any other CASE counts no single thing.
        empty = other is None or (isinstance(other, LiteralExpr) and other.value is None)
        zero = isinstance(other, LiteralExpr) and other.value == 0
        same = all(item.then == first for item in expr.whens)
        if same and (empty or (zero and aggregation == "sum")):
            expr, conditional = first, True
    rows = f"rows of {measure.entity}" + (" where its condition holds" if conditional else "")
    if aggregation == "sum" and isinstance(expr, LiteralExpr) and expr.value == 1:
        return {"count_unit": rows}
    if aggregation not in {"count", "count_distinct"}:
        return {}
    if isinstance(expr, LiteralExpr) and expr.value is not None and aggregation == "count":
        return {"count_unit": rows}
    if not isinstance(expr, ColumnRefExpr):
        return {}
    column = ".".join(part for part in (expr.entity or expr.table, expr.column) if part)
    if aggregation == "count":
        return {"count_unit": f"{rows} with a non-NULL {column}"}
    return {"count_unit": f"distinct {column} across {rows}"}


def _metric_measure_ids(
    config: PackageConfig, expr: SemanticExpr, *, seen_metric_ids: set[str] | None = None
) -> list[str]:
    seen_metric_ids = set(seen_metric_ids or set())
    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        return [expr.measure]
    if isinstance(expr, MetricRecipeRefExpr):
        if expr.metric_recipe in seen_metric_ids:
            return []
        recipe = {row.id: row for row in config.metric_recipes}.get(expr.metric_recipe)
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
    if isinstance(expr, (BooleanExpr, CallExpr)):
        out: list[str] = []
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
    primary_measure = {row.id: row for row in config.measures}[measure_ids[0]]
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
