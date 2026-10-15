"""Public capabilities surface and expression-shape metadata."""

from __future__ import annotations

from typing import Any

from ..dialects import dialect_for_warehouse
from ..expressions import (
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ComparisonExpr,
    ConversionExpr,
    DistributionExpr,
    EntityValueExpr,
    LiteralExpr,
    MetricPredicateExpr,
    RatioExpr,
    ScopedAggregateExpr,
    SemanticExpr,
)
from ..query_call import EXPRESSION_SHAPES
from ..runtime import Runtime, runtime_request_scope
from ..schema import PackageConfig


def _expr_kinds(expr: SemanticExpr) -> set[str]:
    kinds = {type(expr).__name__}
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return kinds | _expr_kinds(expr.left) | _expr_kinds(expr.right)
    if isinstance(expr, BooleanExpr):
        for arg in expr.args:
            kinds |= _expr_kinds(arg)
        return kinds
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            kinds |= _expr_kinds(arg)
        return kinds
    if isinstance(expr, CaseExpr):
        for item in expr.whens:
            kinds |= _expr_kinds(item.when) | _expr_kinds(item.then)
        if expr.else_expr is not None:
            kinds |= _expr_kinds(expr.else_expr)
        return kinds
    if isinstance(expr, MetricPredicateExpr):
        return kinds | _expr_kinds(expr.input)
    if isinstance(expr, ScopedAggregateExpr):
        return kinds
    if isinstance(expr, RatioExpr):
        return kinds | _expr_kinds(expr.numerator) | _expr_kinds(expr.denominator)
    if isinstance(expr, EntityValueExpr):
        return kinds | _expr_kinds(expr.input)
    if isinstance(expr, DistributionExpr):
        return kinds | _expr_kinds(expr.over)
    if isinstance(expr, ConversionExpr):
        return kinds | _expr_kinds(expr.base) | _expr_kinds(expr.converted)
    if isinstance(expr, LiteralExpr):
        return kinds
    return kinds


def _capability_payload(config: PackageConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expr_kinds = set()
    for recipe in config.metric_recipes:
        expr_kinds |= _expr_kinds(recipe.expression)
    recipe_ids = {row.id for row in config.metric_recipes}
    measure_aggs = {agg for measure in config.measures for agg in measure.allowed_aggregations}
    supported = {
        "historical_joins": any(row.temporal_validity for row in config.relationships),
        "point_in_time_metrics": any(row.kind == "semi_additive" for row in config.metric_recipes)
        or any(row.measure_class in {"semi_additive", "snapshot"} for row in config.measures),
        "rolling_windows": any("rolling_" in row for row in recipe_ids),
        "prior_period_offsets": any(
            "prior_period" in row or "month_over_month" in row for row in recipe_ids
        ),
        "period_to_date_metrics": any(
            row.endswith("_mtd") or row.endswith("_qtd") or row.endswith("_ytd")
            for row in recipe_ids
        ),
        "metric_predicates": True,
        "scoped_aggregates": True,
        "runtime_ratios": True,
        "entity_grain_distributions": True,
        "cross_domain_metric_predicates": True,
        "conversion_metrics": "ConversionExpr" in expr_kinds,
        "percentile_metrics": "percentile" in measure_aggs
        or "median" in measure_aggs
        or any("p95" in row or "median" in row for row in recipe_ids),
        # Every fill uses the warehouse's implicit Gregorian calendar.
        "dense_fill": dialect_for_warehouse(config.package.warehouse).has_implicit_calendar,
        "alternate_calendars": False,
    }
    reasons = {
        "conversion_metrics": "no conversion metrics are published"
        if "ConversionExpr" not in expr_kinds
        else "",
        "alternate_calendars": "authored non-default calendars are not supported in this release",
        "period_to_date_metrics": "no PTD metric family is published"
        if not supported["period_to_date_metrics"]
        else "",
        "prior_period_offsets": "no prior-period metric family is published"
        if not supported["prior_period_offsets"]
        else "",
        "rolling_windows": "no rolling metric family is published"
        if not supported["rolling_windows"]
        else "",
        "percentile_metrics": "no percentile or median metric family is published"
        if not supported["percentile_metrics"]
        else "",
    }
    supported_rows = [{"kind": "query_call_v2", "available": True, "reason": ""}] + [
        {"kind": kind, "available": True, "reason": ""} for kind, ok in supported.items() if ok
    ]
    unsupported_rows = [
        {"kind": kind, "available": False, "reason": reasons.get(kind, "not supported")}
        for kind, ok in supported.items()
        if not ok
    ]
    return supported_rows, unsupported_rows


@runtime_request_scope
def capabilities_payload(
    runtime: Runtime, *, policy_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Public capabilities envelope — the cheap-orientation surface."""

    supported, unsupported = _capability_payload(runtime._config)
    return {
        "package_id": runtime.package_id,
        "schema_version": runtime._config.version,
        "capabilities": supported,
        "unsupported_capabilities": unsupported,
        "expression_shapes": [dict(shape) for shape in EXPRESSION_SHAPES],
    }


__all__ = ["_capability_payload", "capabilities_payload"]
