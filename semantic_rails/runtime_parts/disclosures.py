"""Say what a row's numbers cover when they would read as something else.

Both disclosures read the compiled plan's select expressions; neither changes the SQL or the rows.

- ``MIXED_TIME_ROLES`` (a warning): the selects read measures of two or more entities dated by
  different time roles, and the query has no window and no time grain, so each measure covers
  all of its own history. A measure with no time role is its own clock. A governed metric counts
  as one clock, however many it combines: the package defined it, so it warns only beside a
  measure or metric on another clock.
- An ``assumptions`` entry for an ``avg`` (or ``min``, ``max``, ``median``, ``percentile``) of a
  measure whose rows have a parent the output doesn't group by: an entity strictly between the
  measure's rows and a ``group_by`` dimension's entity, or, with no ``group_by``, any declared
  many-to-one parent. For an ``avg`` it adds the per-parent average, as a ratio over the count
  measure of that parent's key, when the package has one. A governed metric is the package's
  definition, so this disclosure doesn't look inside one.

Measures inside conversions and metric predicates follow their own time and grain rules, so
neither disclosure reads them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from ..compiler_parts.bind import _measure_count_distinct_key_columns
from ..diagnostics import semantic_issue
from ..expressions import (
    AggregateExpr,
    ConversionExpr,
    DistributionExpr,
    EntityValueExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    parse_semantic_expression,
    resolve_measure_temporal_role,
)
from ..fanout import entity_label
from ..ir import LogicalPlan
from ..schema import MeasureConfig, MetricConfig, PackageConfig, RelationshipConfig

MIXED_TIME_ROLES = "MIXED_TIME_ROLES"

_GRAIN_AGGREGATIONS = frozenset({"avg", "min", "max", "median", "percentile"})
_LEAVES = (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr, MetricRecipeRefExpr)
# A conversion and a metric predicate keep their own time and grain rules.
_OWN_RULES: tuple[type, ...] = (ConversionExpr, MetricPredicateExpr)
# An entity value (and a distribution over one) aggregates per entity first, at its own grain.
_OWN_GRAIN: tuple[type, ...] = (*_OWN_RULES, EntityValueExpr, DistributionExpr)

MeasureLeaf = MeasureRefExpr | AggregateExpr | ScopedAggregateExpr


def _leaves(node: Any, opaque: tuple[type, ...]) -> Iterator[MeasureLeaf | MetricRecipeRefExpr]:
    """Each measure and governed metric an expression reads, outside ``opaque`` subtrees."""
    if isinstance(node, _LEAVES):
        yield node
    elif is_dataclass(node) and not isinstance(node, opaque):
        for item in fields(node):
            value = getattr(node, item.name)
            for child in value if isinstance(value, (list, tuple)) else (value,):
                yield from _leaves(child, opaque)


def _selects(plan: LogicalPlan) -> list[SemanticExpr]:
    return [
        parse_semantic_expression(payload, context="query")
        for payload in plan.post_aggregation_exprs.values()
    ]


def _measures(config: PackageConfig, plan: LogicalPlan) -> dict[str, MeasureConfig]:
    """The package's measures plus this query's ``aggregate_if`` measures."""
    return {**{row.id: row for row in config.measures}, **plan.synthetic_measures}


@dataclass(frozen=True)
class _Clock:
    object_id: str  # the measure or governed metric; "" for an aggregate_if
    subject: str  # how the message names it
    source: str  # the measure's entity, or the governed metric
    roles: tuple[str, ...]
    clocks: frozenset[str]  # its time roles, and each measure with none as its own clock


def _metric_measures(expr: SemanticExpr, recipes: dict[str, MetricConfig]) -> Iterator[MeasureLeaf]:
    for leaf in _leaves(expr, _OWN_RULES):
        if isinstance(leaf, MetricRecipeRefExpr):
            if (recipe := recipes.get(leaf.metric_recipe)) is not None:
                yield from _metric_measures(recipe.expression, recipes)
        else:
            yield leaf


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
    clocks: set[str] = set()
    for part in parts:
        if (part_measure := measures.get(part.measure)) is None:
            continue
        # The role binding gives the measure (its own, an override, or the query's).
        role = resolve_measure_temporal_role(
            part_measure,
            part.temporal_role,
            plan.query.get("temporal_role_overrides") or {},
            str(plan.time.get("temporal_role") or ""),
        )
        roles.update([role] if role else [])
        clocks.add(role or part_measure.id)
    if not clocks:
        return None
    return _Clock(object_id, subject, source, tuple(sorted(roles)), frozenset(clocks))


def _clock_text(clock: _Clock) -> str:
    if not clock.roles:
        return f"{clock.subject} has no time role"
    unclocked = ["no time role"] if len(clock.clocks) > len(clock.roles) else []
    return f"{clock.subject} by {' and '.join([*clock.roles, *unclocked])}"


def mixed_time_role_warnings(config: PackageConfig, plan: LogicalPlan) -> list[dict[str, Any]]:
    """One ``MIXED_TIME_ROLES`` warning when unwindowed, ungrained selects mix entities' clocks.

    With a window each measure covers the same period on its own clock, and with a time grain
    each row is one period, so neither answer covers all of a measure's history.
    """
    time = plan.time
    if time.get("start") is not None or time.get("end") is not None or time.get("grain"):
        return []
    measures = _measures(config, plan)
    recipes = {row.id: row for row in config.metric_recipes}
    found: dict[_Clock, None] = {}
    for expr in _selects(plan):
        for leaf in _leaves(expr, _OWN_RULES):
            if (clock := _clock(leaf, measures, recipes, plan)) is not None:
                found[clock] = None
    rows = list(found)
    if not any(a.source != b.source and a.clocks != b.clocks for a in rows for b in rows):
        return []
    return [
        semantic_issue(
            code=MIXED_TIME_ROLES,
            message=(
                "These measures are dated by different time roles: "
                f"{'; '.join(_clock_text(clock) for clock in rows)}. With no window each covers "
                "all of its own history; add a window, or read them separately."
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


def _parent_across(rel: RelationshipConfig, entity: str) -> str:
    """The entity one hop from ``entity`` reaches when many of its rows share one row there."""
    if rel.temporal_validity:
        return ""
    near, _, far = (part.strip() for part in rel.cardinality.upper().partition(":"))
    if entity == rel.source_entity:
        other = rel.target_entity
    elif entity == rel.target_entity:
        near, far, other = far, near, rel.source_entity
    else:
        return ""
    return other if (near, far) == ("N", "1") and other != entity else ""


def _grouped_parents(config: PackageConfig, plan: LogicalPlan, measure: MeasureConfig) -> list[str]:
    """Entities strictly between the measure's rows and its ``group_by`` dimensions' entities."""
    relationships = {row.id: row for row in config.relationships}
    dimensions = {row.id: row for row in config.dimensions}
    grouped = {dimensions[dim_id].entity for dim_id in plan.group_by if dim_id in dimensions}
    parents: list[str] = []
    for row in plan.measure_plans:
        if row.bound_measure.measure_id != measure.id:
            continue
        for selection in row.path_selections:
            if selection.purpose != "group_by":
                continue
            current = measure.entity
            for rel_id in selection.chosen_path:
                current = _parent_across(relationships[rel_id], current)
                if not current:
                    break
                if current not in grouped:
                    parents.append(current)
    return list(dict.fromkeys(parents))


def _count_measure(config: PackageConfig, entity: str) -> str:
    """The id of a measure that counts ``entity``'s single-column key, or ""."""
    return next(
        (
            row.id
            for row in sorted(config.measures, key=lambda row: row.id)
            if row.entity == entity and len(_measure_count_distinct_key_columns(row, config)) == 1
        ),
        "",
    )


def _per_parent_hint(
    config: PackageConfig, leaf: MeasureLeaf, measure: MeasureConfig, parent: str
) -> str:
    """The ratio IR of the per-``parent`` average, when the package counts ``parent``."""
    plain = isinstance(leaf, MeasureRefExpr) or (
        isinstance(leaf, AggregateExpr) and not (leaf.filter or leaf.window)
    )
    count = _count_measure(config, parent)
    if not (
        plain
        and count
        and not (leaf.temporal_role or leaf.parameters)
        and measure.measure_class == "additive"
        and "sum" in measure.allowed_aggregations
    ):
        return ""
    ratio = {
        "kind": "ratio",
        "numerator": {"kind": "aggregate", "measure": measure.id, "aggregation": "sum"},
        "denominator": {"kind": "aggregate", "measure": count},
    }
    return (
        f"for a per-{entity_label(config, parent)} average select "
        f"{json.dumps(ratio, separators=(',', ':'))}"
    )


def averaging_grain_assumptions(config: PackageConfig, plan: LogicalPlan) -> list[str]:
    """Say which rows an average (or min, max, median, percentile) of a measure runs over."""
    measures = _measures(config, plan)
    entries: list[str] = []
    for expr in _selects(plan):
        for leaf in _leaves(expr, _OWN_GRAIN):
            if isinstance(leaf, MetricRecipeRefExpr):
                continue
            measure = measures.get(leaf.measure)
            if measure is None:
                continue
            aggregation = (leaf.aggregation or measure.default_aggregation).lower()
            if aggregation not in _GRAIN_AGGREGATIONS:
                continue
            parents = (
                _grouped_parents(config, plan, measure)
                if plan.group_by
                else [
                    parent
                    for rel in config.relationships
                    if (parent := _parent_across(rel, measure.entity))
                ]
            )
            if not parents:
                continue
            subject = (
                measure.label
                if measure.id in plan.synthetic_measures
                else f"{aggregation}({measure.id})"
            )
            verb = "averages over" if aggregation == "avg" else "is taken over"
            hints = (
                [
                    hint
                    for parent in dict.fromkeys(parents)
                    if (hint := _per_parent_hint(config, leaf, measure, parent))
                ]
                if aggregation == "avg" and measure.id not in plan.synthetic_measures
                else []
            )
            entries.append(
                "; ".join([f"{subject} {verb} {entity_label(config, measure.entity)} rows", *hints])
                + "."
            )
    return list(dict.fromkeys(entries))
