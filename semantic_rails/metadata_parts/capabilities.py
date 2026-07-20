"""Public capabilities surface and expression-shape metadata."""

from __future__ import annotations

from typing import Any

from ..expressions import (
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ComparisonExpr,
    ConversionExpr,
    CumulativeExpr,
    DistributionExpr,
    EntityValueExpr,
    LiteralExpr,
    MetricPredicateExpr,
    PeriodToDateExpr,
    PriorPeriodExpr,
    RatioExpr,
    RollingExpr,
    ScopedAggregateExpr,
    SemanticExpr,
)
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
    if isinstance(expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr)):
        return kinds | _expr_kinds(expr.input)
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
    calendar_ids = {row.calendar_id for row in config.entities if row.calendar_id}
    supported = {
        "historical_joins": any(row.temporal_validity for row in config.relationships),
        "point_in_time_metrics": any(row.kind == "semi_additive" for row in config.metric_recipes)
        or any(row.measure_class in {"semi_additive", "snapshot"} for row in config.measures),
        "rolling_windows": "RollingExpr" in expr_kinds
        or any("rolling_" in row for row in recipe_ids),
        "prior_period_offsets": "PriorPeriodExpr" in expr_kinds
        or any("prior_period" in row or "month_over_month" in row for row in recipe_ids),
        "period_to_date_metrics": "PeriodToDateExpr" in expr_kinds
        or any(
            row.endswith("_mtd") or row.endswith("_qtd") or row.endswith("_ytd")
            for row in recipe_ids
        ),
        "metric_predicates": True,
        "scoped_aggregates": True,
        "runtime_ratios": True,
        "entity_grain_distributions": True,
        "cross_domain_metric_predicates": True,
        "agent_query_ir_v2": True,
        "conversion_metrics": "ConversionExpr" in expr_kinds,
        "percentile_metrics": "percentile" in measure_aggs
        or "median" in measure_aggs
        or any("p95" in row or "median" in row for row in recipe_ids),
        "dense_fill": any(row.kind == "time" for row in config.entities),
        "alternate_calendars": len(calendar_ids) > 1,
    }
    reasons = {
        "conversion_metrics": "no conversion metrics are published"
        if "ConversionExpr" not in expr_kinds
        else "",
        "alternate_calendars": "only the default calendar is declared"
        if not supported["alternate_calendars"]
        else "",
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
    supported_rows = [
        {"kind": kind, "available": True, "reason": ""} for kind, ok in supported.items() if ok
    ]
    unsupported_rows = [
        {"kind": kind, "available": False, "reason": reasons.get(kind, "not supported")}
        for kind, ok in supported.items()
        if not ok
    ]
    return supported_rows, unsupported_rows


_EXPRESSION_SHAPES: tuple[dict[str, Any], ...] = (
    {
        "name": "aggregate",
        "description": "raw measure aggregate",
        "example": {"aggregation": "sum", "measure": "measure.<id>"},
    },
    {
        "name": "metric",
        "description": "governed metric recipe",
        "example": {"metric": "metric.<id>"},
    },
    {
        "name": "prior_period",
        "description": "windowed prior period",
        "example": {
            "kind": "prior_period",
            "measure": "measure.<id>",
            "offset": -1,
            "grain": "month",
        },
    },
    {
        "name": "rolling",
        "description": "rolling window. window is an object {unit, value} — not an int.",
        "example": {
            "kind": "rolling",
            "input": {"measure": "measure.<id>"},
            "window": {"unit": "day", "value": 7},
        },
    },
    {
        "name": "cumulative",
        "description": (
            "running total. grain comes from the outer query.time.grain — "
            "cumulative itself takes only input + optional partition_by/window_scope."
        ),
        "example": {"kind": "cumulative", "input": {"measure": "measure.<id>"}},
    },
    {
        "name": "ratio",
        "description": "derived ratio",
        "example": {
            "kind": "ratio",
            "numerator": {"measure": "measure.<num>"},
            "denominator": {"measure": "measure.<den>"},
        },
    },
    {
        "name": "conversion",
        "description": (
            "event-pair conversion. Anchored to a single entity; window is an "
            "object {unit, value}; matching_mode is required. base/converted "
            "operands accept an aggregate with a 'filter' of dimension "
            "predicates ({all: [{field, op, value}]}) to restrict each side — "
            "e.g. base = orders containing Product A, converted = orders "
            "containing Product B; the filters are applied in the event CTEs "
            "with EXISTS semantics. Operand-level 'window' specs and "
            "expression-valued filter clauses are rejected (use query-level "
            "metric_filters for those)."
        ),
        "example": {
            "kind": "conversion",
            "base": {
                "kind": "aggregate",
                "measure": "measure.<start>",
                "filter": {"all": [{"field": "dimension.<id>", "op": "=", "value": "<value>"}]},
            },
            "converted": {"measure": "measure.<end>"},
            "entity": "entity.<id>",
            "window": {"unit": "day", "value": 7},
            "matching_mode": "first_converted_after_base",
        },
    },
    {
        "name": "distribution",
        "description": (
            "distribution view over entity-grain values. `over` must be an "
            "entity_value expression; `function` selects the aggregator "
            "(e.g. percentile)."
        ),
        "example": {
            "kind": "distribution",
            "function": "percentile",
            "over": {
                "kind": "entity_value",
                "entity": "entity.<id>",
                "input": {"measure": "measure.<id>"},
            },
            "p": 0.5,
        },
    },
    {
        "name": "aggregate_if",
        "description": (
            "shorthand for <AGG>(CASE WHEN cond THEN value END). Column refs "
            "inside `condition` / `value` must specify `entity` or `table` "
            "(no surrounding measure to inherit from). Compiles to "
            "COUNT_IF / SUM_IF natively on Snowflake; portable CASE WHEN "
            "elsewhere. `value` may be omitted when aggregation=count."
        ),
        "example": {
            "kind": "aggregate_if",
            "aggregation": "sum",
            "condition": {
                "kind": "comparison",
                "op": "=",
                "left": {"kind": "column", "column": "<col>", "entity": "entity.<id>"},
                "right": {"kind": "literal", "value": True},
            },
            "value": {"kind": "column", "column": "<col>", "entity": "entity.<id>"},
        },
    },
    {
        "name": "between",
        "description": (
            "range predicate sugar; desugars at parse time to "
            "`expr >= low AND expr <= high`. Use `kind: not_between` or "
            "`negated: true` for the inverted form (lowers to "
            "`expr < low OR expr > high`)."
        ),
        "example": {
            "kind": "between",
            "expr": {"kind": "column", "column": "<col>"},
            "low": {"kind": "literal", "value": 0},
            "high": {"kind": "literal", "value": 100},
        },
    },
)


@runtime_request_scope
def capabilities_payload(runtime: Runtime) -> dict[str, Any]:
    """Public capabilities envelope — the cheap-orientation surface."""

    supported, unsupported = _capability_payload(runtime.config)
    return {
        "package_id": runtime.package_id,
        "schema_version": runtime.config.version,
        "capabilities": supported,
        "unsupported_capabilities": unsupported,
        "expression_shapes": [dict(shape) for shape in _EXPRESSION_SHAPES],
    }


__all__ = ["_EXPRESSION_SHAPES", "_capability_payload", "capabilities_payload"]
