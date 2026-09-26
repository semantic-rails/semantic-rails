"""Which declared rollup answers a measure leaf exactly, and why each other one can't.

The planner asks :func:`_select_aggregate_relation` once per measure leaf. Every rollup it
rejects gets a stable reason code, which the routing report (:mod:`.routing`) shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..ast import NormalizedQuery
from ..compiler_parts.bind import (
    _bound_filter_clauses,
    _bound_metric_predicates,
    _measure_count_distinct_key_columns,
)
from ..compiler_parts.indexes import _dimension_index, _measure_index, _temporal_role_index
from ..compiler_parts.paths import (
    _direct_dimension_source_expr,
    _entity_key_dimension_ids,
    _leaf_time_role,
)
from ..compiler_parts.temporal import _fractional_second, _is_grain_boundary, _parse_time_literal
from ..errors import SemanticLayerError
from ..expressions import MetricPredicateExpr
from ..ir import BoundMeasure, PathSelection
from ..schema import AggregateRelationConfig, MeasureConfig, PackageConfig
from .routing import ROUTING_OFF, aggregate_routing_enabled

_ROUTABLE_GRAIN_ORDER = {
    "transaction": 0,
    "minute": 1,
    "hour": 2,
    "day": 3,
    "week": 4,
    "month": 5,
    "quarter": 6,
    "year": 7,
}


def _grain_rank(grain: str) -> int:
    return _ROUTABLE_GRAIN_ORDER.get(str(grain or "").strip().lower(), -1)


def _aggregate_dimension_coverage(row: AggregateRelationConfig) -> set[str]:
    return {str(item) for item in [*row.dimensions, *row.dimension_columns]}


def _aggregate_measure_coverage(row: AggregateRelationConfig) -> set[str]:
    return {str(item) for item in [*row.measures, *row.measure_columns]}


# What a column holding each value re-aggregates with; the query must ask for the same aggregation.
_RECOMBINE = {"sum": "sum", "min": "min", "max": "max", "count_distinct": "sum"}
# What an additive or precomputed column without `holds:` holds, by measure class.
_LEGACY_HOLDS = {
    "additive": "sum",
    "event_count": "count_distinct",
    "distinct_population": "count_distinct",
}


def _time_bound_on_grain(value: Any, grain: str) -> bool:
    """Whether a query time bound, without a UTC offset (or a zero one), starts a ``grain`` bucket.

    A day boundary is also an hour and minute boundary, so finer rollups check the day.
    """
    try:
        moment = _parse_time_literal(value)
        grain = grain if _grain_rank(grain) >= _grain_rank("day") else "day"
        return (
            not moment.utcoffset()
            and not _fractional_second(value)  # datetime drops digits past the microsecond
            and _is_grain_boundary(moment, grain)
        )
    except SemanticLayerError:  # an unparseable bound
        return False


def _leaf_rollup_blocker(
    bound: BoundMeasure, query: NormalizedQuery, config: PackageConfig, leaf_time_role: str
) -> str:
    """Why no rollup can answer this measure leaf exactly, whichever rollup it is."""
    if not aggregate_routing_enabled():
        return ROUTING_OFF
    if _bound_metric_predicates(bound) or any(
        isinstance(item.expression, MetricPredicateExpr) for item in query.metric_filters
    ):
        return "metric_predicate_filter"
    if query.time and (query.time.calendar_id or "default").strip().lower() != "default":
        return "calendar_mismatch"
    role = _temporal_role_index(config).get(leaf_time_role)
    column_tz = str(getattr(role, "column_timezone", "") or "").strip()
    target_tz = str(getattr(role, "timezone", "") or "").strip()
    if column_tz and target_tz and column_tz != target_tz:
        return "timezone_mismatch"  # the base path converts the zone; the rollup path can't
    if _measure_index(config)[bound.measure_id].measure_class in {"semi_additive", "snapshot"}:
        # The base path takes each key's snapshot per period, whatever the aggregation.
        return "aggregation_not_reaggregable"
    return ""


@dataclass(frozen=True)
class _Leaf:
    """What every rollup of one measure leaf is checked against."""

    bound: BoundMeasure
    measure: MeasureConfig
    query: NormalizedQuery
    time_role: str
    dimensions: set[str]  # grouped, `where` and measure-filter dimensions
    single_valued: set[str]  # grouped, or pinned to one value by an equality filter
    # The query's join path to each dimension from another model; None if it can fan out.
    join_paths: dict[str, list[str] | None]
    counts_fact_key: bool


def _column_holds(row: AggregateRelationConfig, measure: MeasureConfig) -> tuple[str, str]:
    """What a rollup column holds per row, or the reason that isn't known."""
    holds = row.measure_holds.get(measure.id, "")
    if not holds:
        if str(row.measure_rollups.get(measure.id, "")).lower() not in {"additive", "precomputed"}:
            return "", "unsupported_rollup"
        holds = _LEGACY_HOLDS.get(measure.measure_class, "")
        if not holds:
            return "", "aggregation_not_reaggregable"
    aggregation = str(row.measure_aggregations.get(measure.id, "") or "").lower()
    if aggregation and aggregation != _RECOMBINE[holds]:
        return "", "unsupported_rollup_aggregation"
    return holds, ""


def recombine_aggregation(row: AggregateRelationConfig, measure_id: str) -> str:
    """The aggregation that re-aggregates a routed measure column."""
    return _RECOMBINE.get(row.measure_holds.get(measure_id, ""), "sum")


def _one_row_per_group(row: AggregateRelationConfig, leaf: _Leaf, config: PackageConfig) -> bool:
    """Whether each output row is exactly one rollup row: the query's grain is the rollup's,
    and every rollup dimension, including the keys of its entity grain, is single-valued."""
    try:
        keys = {
            dim for entity in row.entity_grain for dim in _entity_key_dimension_ids(entity, config)
        }
    except SemanticLayerError:  # an entity without a key dimension can't be grouped
        return False
    dimensions = _aggregate_dimension_coverage(row)
    return (
        leaf.query.time is not None
        and str(leaf.query.time.grain or "").lower() == row.grain
        and keys <= dimensions
        and dimensions <= leaf.single_valued
    )


def _aggregate_relation_rejection_reason(
    row: AggregateRelationConfig, leaf: _Leaf, config: PackageConfig
) -> str:
    if row.source != "default":
        return "non_default_source"
    if (row.equivalence_kind or "exact") != "exact":
        return "non_exact_equivalence"
    if row.filters:
        # The rollup holds only the rows its filters kept, and no query is proven to imply them.
        return "rollup_filter_not_implied"
    time = leaf.query.time
    requested_grain = str((time.grain if time else "") or "").lower()
    if time is None or not requested_grain:
        return "missing_query_time_grain"
    if row.temporal_role and row.temporal_role != leaf.time_role:
        return "temporal_role_mismatch"
    if requested_grain not in set(row.eligible_time_grains or [row.grain]):
        return "unsupported_query_grain"
    if _grain_rank(row.grain) < 0 or _grain_rank(row.grain) > _grain_rank(requested_grain):
        return "aggregate_grain_too_coarse"
    if row.grain == "week" and requested_grain != "week":
        # Weeks straddle month, quarter and year boundaries.
        return "non_nesting_grain"
    if not all(
        _time_bound_on_grain(value, row.grain)
        for value in (time.start, time.end)
        if value is not None
    ):
        return "time_bounds_not_aligned"
    measure_id = leaf.bound.measure_id
    if measure_id not in _aggregate_measure_coverage(row):
        return "missing_measure"
    if measure_id not in row.measure_columns:
        return "missing_measure_column"
    if leaf.dimensions - _aggregate_dimension_coverage(row):
        return "missing_dimension"
    if any(
        path is None or row.dimension_paths.get(dim) != path
        for dim, path in leaf.join_paths.items()
    ):
        # A pre-joined column is right only along the query's own many-to-one path.
        return "join_path_mismatch"
    holds, reason = _column_holds(row, leaf.measure)
    if reason:
        return reason
    query_aggregation = str(leaf.bound.aggregation or "").lower()
    if query_aggregation not in _RECOMBINE:
        return "unsupported_query_aggregation"
    if query_aggregation != holds:
        return "aggregation_not_reaggregable"
    # One key can sit in several rollup rows, so distinct counts can't be added up. Only a
    # declared distinct count, one rollup row per output row, is the answer as it is.
    if (
        holds == "count_distinct"
        and not leaf.counts_fact_key
        and not (row.measure_holds.get(measure_id) and _one_row_per_group(row, leaf, config))
    ):
        return "aggregation_not_reaggregable"
    return ""


def _join_paths(
    dimensions: set[str], leaf_entity: str, selections: list[PathSelection], config: PackageConfig
) -> dict[str, list[str] | None]:
    """The query's path to each dimension a rollup would hold pre-joined from another model."""
    chosen = {selection.target_entity: selection for selection in selections}
    paths: dict[str, list[str] | None] = {}
    for dim_id in sorted(dimensions):
        entity = _dimension_index(config)[dim_id].entity
        if entity == leaf_entity or _direct_dimension_source_expr(leaf_entity, dim_id, config):
            continue
        selection = chosen.get(entity)
        paths[dim_id] = None
        if (
            selection is not None
            and selection.analysis.get("status") == "ok"  # every hop many-to-one or one-to-one
            and not any(
                join.get("temporal_validity") for join in selection.analysis["relationships"]
            )
        ):
            paths[dim_id] = list(selection.chosen_path)
    return paths


def _select_aggregate_relation(
    *,
    bound: BoundMeasure,
    query: NormalizedQuery,
    config: PackageConfig,
    path_selections: list[PathSelection],
) -> tuple[str, dict[str, str]]:
    """Pick the rollup that answers a measure leaf exactly, and say why each other one can't."""
    measure = _measure_index(config)[bound.measure_id]
    rows = [row for row in config.aggregate_relations if row.source_entity == measure.entity]
    if not rows:
        return "", {}
    leaf_time_role = _leaf_time_role(bound, query, config)
    blocker = _leaf_rollup_blocker(bound, query, config, leaf_time_role)
    if blocker:
        return "", {row.id: blocker for row in rows}
    filters = [
        *((item.field, item.op, item.value) for item in query.where),
        *(
            (item["field"], item["op"], item["value"])
            for item in _bound_filter_clauses(bound, config)
        ),
    ]
    dimensions = {str(item) for item in query.group_by} | {str(field) for field, _, _ in filters}
    dimensions.discard("")
    key = _measure_count_distinct_key_columns(measure, config)
    leaf = _Leaf(
        bound=bound,
        measure=measure,
        query=query,
        time_role=leaf_time_role,
        dimensions=dimensions,
        single_valued={str(item) for item in query.group_by}
        | {
            str(field)
            for field, op, value in filters
            if str(op or "=").strip() in {"=", "=="} and not isinstance(value, (list, tuple, set))
        },
        join_paths=_join_paths(dimensions, measure.entity, path_selections, config),
        # Each value of a model's single-column row key sits in one rollup row.
        counts_fact_key=len(key) == 1 and measure.row_grain == key and not measure.source_relation,
    )
    rejections = {
        row.id: reason
        for row in rows
        if (reason := _aggregate_relation_rejection_reason(row, leaf, config))
    }
    candidates = [row for row in rows if row.id not in rejections]
    candidates.sort(
        key=lambda row: (
            _grain_rank(row.grain),
            -len(row.entity_grain),
            int(row.selection_priority or 0),
            row.id,
        ),
        reverse=True,
    )
    return (candidates[0].id if candidates else ""), rejections
