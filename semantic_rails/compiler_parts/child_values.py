"""A grouping by a child's values after a lookup: a one-to-many hop at the end of a route from
the measure's own rows.

"Room area by the city of each room's building": a room looks up its building, and a building
has several addresses, in one city or more. From the rooms that is a lookup before a
one-to-many hop, so no one set of addresses belongs to each room row, and the de-duplicated
leaf, which keeps one row per room key, refuses it (``_dedup_refusal``; a keyless model has no
key to keep). Where that route is decided (the call's own, the package's recorded one, or the
only one) and walkable from its one side, the leaf reads the measure's rows as they are
stored, each hop joined LEFT, against the child's distinct values per parent
(``child_values_cte``: ``SELECT DISTINCT <the child's columns to the parent>, <its grouped
columns> FROM <child>``). Each stored row counts once, with its whole value, in every group of
values its parent holds: two child rows with the same values count once, and a row whose
parent has no child row, or that names no parent, is the NULL group. The groups overlap, so
they don't add up to the total; a total is the call without the grouping.

``child_values_route`` says which leaves read this way; anything else keeps its refusal.
"""

from __future__ import annotations

from typing import Any

from ..ast import NormalizedQuery, child_groups, plain_filters
from ..fanout import entity_label, route_reading
from ..ir import BoundMeasure, MeasurePlan, PathSelection, RewriteStep
from ..schema import MeasureConfig, PackageConfig
from ..sql_ast import SqlCte, SqlField, SqlJoin, SqlSelect, SqlTableRef
from .bind import _bound_metric_predicates, is_conditional_aggregate
from .indexes import _dimension_index, _entity_index, _measure_index, _relationship_index
from .lowering_common import _edge_cap
from .paths import _column_ref, _outer_joins, unknown_as_null
from .plan_checks import (
    _WHOLE_GROUPED_AGGREGATIONS,
    _beside_another_entity,
    _is_count,
    _overlapping_share,
)

CHILD_VALUES = "child_values"


def _lookups_then_one_to_many(analysis: dict[str, Any], keys: dict[str, list[str]]) -> bool:
    """Whether a route is one or more lookups, then one plain one-to-many hop, last: the
    reverse of N:1 or a forward 1:N, with no validity window and no link, whose one side joins
    on exactly its declared key (as ``one_to_many_descent`` requires of a descent)."""
    rows = list(analysis.get("relationships", []) or [])
    if len(rows) < 2 or any(row.get("temporal_validity") or row.get("link") for row in rows):
        return False
    *lookups, last = rows
    if any(row.get("directional_safety") != "safe" for row in lookups):
        return False
    forward = last.get("traversal") == "forward"
    side = "source" if forward else "target"
    columns = list(last.get(f"{side}_columns") or [last.get(f"{side}_column")])
    return (
        last.get("directional_safety") == "requires_rewrite"
        and str(last.get("cardinality", "")).upper().replace(" ", "")
        == ("1:N" if forward else "N:1")
        and sorted(columns) == sorted(keys.get(str(last.get(f"{side}_entity", "")), []))
    )


def child_values_route(
    measure: MeasureConfig,
    bound: BoundMeasure,
    selections: list[PathSelection],
    config: PackageConfig,
    query: NormalizedQuery,
) -> PathSelection | None:
    """The leaf's one grouped route that ends in a one-to-many hop after its lookups, when the
    leaf reads it through the child's distinct values (see the module docstring); None when it
    doesn't. Only the call's ``group_by`` crosses that hop: a filter there, a time window, a
    result filter or an entity filter keeps the leaf's refusal, as does a measure that isn't a
    sum, count, average or distinct count of an additive, non-stock value with no window, no
    share over the grouping and no other entity's measure in its item."""
    crossing = [row for row in selections if row.analysis.get("status") != "ok"]
    aggregation = str(bound.aggregation or measure.default_aggregation or "").lower()
    if (
        query.time is not None
        or query.metric_filters
        or child_groups(query.where)
        or len(crossing) != 1
        or crossing[0].purpose != "group_by"
        or aggregation not in _WHOLE_GROUPED_AGGREGATIONS
        or not measure.additive
        or measure.measure_class in {"semi_additive", "snapshot"}
        or measure.lookup_from
        or is_conditional_aggregate(measure)
        or bound.window_spec
        or _bound_metric_predicates(bound)
        or _edge_cap(measure, "", config)
        or _beside_another_entity(measure, config, query)
        or _overlapping_share(measure, bound, selections, config, query)
    ):
        return None
    route = crossing[0]
    keys = {entity.id: list(entity.key_columns) for entity in config.entities}
    dimensions = _dimension_index(config)
    filtered = {dimensions[item.field].entity for item in plain_filters(query.where)}
    if route.target_entity in filtered or not _lookups_then_one_to_many(route.analysis, keys):
        return None
    return route


def _child_route(measure_plan: MeasurePlan) -> PathSelection:
    (route,) = [row for row in measure_plan.path_selections if row.analysis.get("status") != "ok"]
    return route


def child_values_cte(
    measure_plan: MeasurePlan, group_by: list[str], config: PackageConfig
) -> tuple[str, SqlCte]:
    """The child entity a ``child_values`` leaf groups by, and the CTE of its distinct values
    per parent: the child's columns to its parent, then each grouped column of the child."""
    route = _child_route(measure_plan)
    child = _entity_index(config)[route.target_entity]
    rel = _relationship_index(config)[route.chosen_path[-1]]
    to_parent = (
        list(rel.source_columns or [rel.source_column])
        if rel.source_entity == child.id
        else list(rel.target_columns or [rel.target_column])
    )
    dimensions = _dimension_index(config)
    grouped = [
        dimensions[dim_id].column for dim_id in group_by if dimensions[dim_id].entity == child.id
    ]
    columns = list(dict.fromkeys([*to_parent, *grouped]))
    name = f"{measure_plan.cte_name}_child_values"
    values = SqlSelect(
        select=[SqlField(_column_ref(child.table, column), column) for column in columns],
        from_table=SqlTableRef(name=child.table),
        distinct=True,
    )
    return child.id, SqlCte(name=name, query=values)


def child_value_dimension(dim_id: str, child: str, cte: str, config: PackageConfig) -> Any | None:
    """A grouped dimension of the child read from its distinct values (``cte``), or None for any
    other dimension."""
    dim = _dimension_index(config)[dim_id]
    if dim.entity != child:
        return None
    return unknown_as_null(_column_ref(cte, dim.column), dim)


def keep_rows(joins: list[SqlJoin], cte: str, config: PackageConfig) -> list[SqlJoin]:
    """``joins`` with the one to the child's values LEFT: a row whose parent has none is the
    NULL group. A warehouse whose outer join reads a type default for an unmatched row
    (ClickHouse) joins it INNER, as it joins lookups: there such a row is in no group."""
    if not _outer_joins(config):
        return joins
    return [
        SqlJoin("LEFT", join.table, join.on)
        if isinstance(join.table, SqlTableRef) and join.table.name == cte
        else join
        for join in joins
    ]


def child_values_step(
    config: PackageConfig, query: NormalizedQuery, bound: BoundMeasure, route: PathSelection
) -> RewriteStep:
    """The disclosure of a grouping by a child's values after a lookup: each row counts once in
    every group of values its parent holds, so the groups overlap and don't add up to the
    total; a row whose parent has none is the NULL group; a total counts each row once."""
    measure = _measure_index(config)[bound.measure_id]
    label = entity_label(config, measure.entity)
    # A model with no key is named by its rows: "each row of Room rows".
    row = (
        f"row of {label}" if _entity_index(config)[measure.entity].is_row_grain else f"{label} row"
    )
    parent_entity = (
        route.analysis["relationships"][-1]["target_entity"]
        if route.analysis["relationships"][-1].get("traversal") == "reverse"
        else route.analysis["relationships"][-1]["source_entity"]
    )
    parent, child = entity_label(config, parent_entity), entity_label(config, route.target_entity)
    dimensions = _dimension_index(config)
    grouped = [
        dim_id for dim_id in query.group_by if dimensions[dim_id].entity == route.target_entity
    ]
    overlap: dict[str, Any] = {"group_by": grouped, "relationships": [route.chosen_path[-1]]}
    if not _is_count(measure, str(bound.aggregation or measure.default_aggregation).lower()):
        overlap["whole_amounts"] = True
    return RewriteStep(
        kind=CHILD_VALUES,
        status="applied",
        measure_id=bound.measure_id,
        reason=(
            f"Grouped by {', '.join(repr(dim_id) for dim_id in grouped)} from "
            f"{route_reading(config, measure.entity, route.chosen_path)}: each {row} counts "
            f"once, with its whole value, in every group of values its {parent}'s {child} rows "
            f"hold ({child} rows with the same values count once), so the groups overlap and "
            f"don't add up to the total, and a {row} whose {parent} has no {child} row, or "
            f"that names no {parent}, is the NULL group. A total (totals, or a total item) is "
            f"the call without the grouping: it counts each {row} once."
        ),
        details={"path": list(route.chosen_path), "overlapping_groups": overlap},
    )
