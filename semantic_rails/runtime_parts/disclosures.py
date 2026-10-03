"""Say what a row's numbers cover when they would read as something else.

``MIXED_TIME_ROLES`` (a warning) reads the compiled plan's select expressions and never changes
the SQL or the rows: the selects read measures of two or more entities dated by different time
roles, and the query has no time block. Each period is read on its own role's clock, and
measure-level filters can bound it. Undated measures are not clocks. A governed metric counts
as one clock, however many it combines: the package defined it, so it warns only beside a
measure or metric on another clock. Measures inside conversions and metric predicates keep
their own time rules, so the warning doesn't read them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from ..diagnostics import semantic_issue
from ..expressions import (
    AggregateExpr,
    ConversionExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    parse_semantic_expression,
    resolve_measure_temporal_role,
)
from ..ir import LogicalPlan
from ..schema import MeasureConfig, MetricConfig, PackageConfig

MIXED_TIME_ROLES = "MIXED_TIME_ROLES"

_LEAVES = (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr, MetricRecipeRefExpr)
# A conversion and a metric predicate keep their own time rules.
_OWN_RULES = (ConversionExpr, MetricPredicateExpr)

MeasureLeaf = MeasureRefExpr | AggregateExpr | ScopedAggregateExpr


def _leaves(node: Any) -> Iterator[MeasureLeaf | MetricRecipeRefExpr]:
    """Each measure and governed metric an expression reads, outside conversions and predicates."""
    if isinstance(node, _LEAVES):
        yield node
    elif is_dataclass(node) and not isinstance(node, _OWN_RULES):
        for item in fields(node):
            value = getattr(node, item.name)
            for child in value if isinstance(value, (list, tuple)) else (value,):
                yield from _leaves(child)


def _metric_measures(expr: SemanticExpr, recipes: dict[str, MetricConfig]) -> Iterator[MeasureLeaf]:
    for leaf in _leaves(expr):
        if isinstance(leaf, MetricRecipeRefExpr):
            if (recipe := recipes.get(leaf.metric_recipe)) is not None:
                yield from _metric_measures(recipe.expression, recipes)
        else:
            yield leaf


@dataclass(frozen=True)
class _Clock:
    object_id: str  # the measure or governed metric; "" for an aggregate_if
    subject: str  # how the message names it
    source: str  # the measure's entity, or the governed metric
    roles: tuple[str, ...]


def _clock(
    leaf: MeasureLeaf | MetricRecipeRefExpr,
    measures: dict[str, MeasureConfig],
    recipes: dict[str, MetricConfig],
    plan: LogicalPlan,
) -> _Clock | None:
    if isinstance(leaf, MetricRecipeRefExpr):
        recipe = recipes.get(leaf.metric_recipe)
        parts = [] if recipe is None else list(_metric_measures(recipe.expression, recipes))
        object_id = subject = source = leaf.metric_recipe
    elif (measure := measures.get(leaf.measure)) is not None:
        parts = [leaf]
        synthetic = measure.id in plan.synthetic_measures
        object_id = "" if synthetic else measure.id
        subject = measure.label if synthetic else measure.id
        source = measure.entity
    else:
        return None
    roles: set[str] = set()
    for part in parts:
        if (part_measure := measures.get(part.measure)) is None:
            continue
        # The role binding gives the measure (its own, an override, or the query's).
        role = resolve_measure_temporal_role(
            part_measure,
            part.temporal_role,
            plan.query.get("temporal_role_overrides") or {},
            "",
        )
        roles.update([role] if role else [])
    if not roles:
        return None
    return _Clock(object_id, subject, source, tuple(sorted(roles)))


def _clock_text(clock: _Clock) -> str:
    return f"{clock.subject} by {' and '.join(clock.roles)}"


def mixed_time_role_warnings(config: PackageConfig, plan: LogicalPlan) -> list[dict[str, Any]]:
    """One ``MIXED_TIME_ROLES`` warning when selects without a time block mix real time roles.

    Undated measures are ignored. Different nonempty role sets establish at least two
    distinct real roles. Filters may bound each measure's period independently.
    """
    if plan.time:
        return []
    # The package's measures plus this query's aggregate_if measures.
    measures = {**{row.id: row for row in config.measures}, **plan.synthetic_measures}
    recipes = {row.id: row for row in config.metric_recipes}
    found: dict[_Clock, None] = {}
    for payload in plan.post_aggregation_exprs.values():
        for leaf in _leaves(parse_semantic_expression(payload, context="query")):
            if (clock := _clock(leaf, measures, recipes, plan)) is not None:
                found[clock] = None
    rows = list(found)
    if not any(a.source != b.source and a.roles != b.roles for a in rows for b in rows):
        return []
    return [
        semantic_issue(
            code=MIXED_TIME_ROLES,
            message=(
                "These measures are dated by different time roles: "
                f"{'; '.join(_clock_text(clock) for clock in rows)}. "
                "Each period is read on its own role's clock."
            ),
            severity="warning",
            stage="planning",
            details={
                "clocks": [
                    {"subject": clock.subject, "temporal_roles": list(clock.roles)}
                    for clock in rows
                ]
            },
            object_ids=[clock.object_id for clock in rows],
        )
    ]
