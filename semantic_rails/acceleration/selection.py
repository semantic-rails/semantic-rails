"""Which declared rollup answers a measure leaf exactly, and why each other one can't.

The planner asks :func:`_select_aggregate_relation` once per measure leaf. Every rollup it
rejects gets a stable reason code, which the routing report (:mod:`.routing`) shows.
"""

from __future__ import annotations

import re
from typing import Any

from ..ast import NormalizedQuery
from ..compiler_parts.bind import (
    _bound_filter_clauses,
    _bound_metric_predicates,
    _measure_count_distinct_key_columns,
)
from ..compiler_parts.indexes import _measure_index, _temporal_role_index
from ..compiler_parts.paths import _leaf_time_role
from ..compiler_parts.temporal import _is_grain_boundary, _parse_time_literal
from ..errors import SemanticLayerError
from ..expressions import MetricPredicateExpr
from ..ir import BoundMeasure
from ..schema import AggregateRelationConfig, PackageConfig
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


def _time_bound_on_grain(value: Any, grain: str) -> bool:
    """Whether a query time bound, without a UTC offset (or a zero one), starts a ``grain`` bucket.

    A day boundary is also an hour and minute boundary, so finer rollups check the day.
    """
    if re.search(r"[.,]\d{7}", str(value)):  # datetime drops digits past the microsecond
        return False
    try:
        moment = _parse_time_literal(value)
        grain = grain if _grain_rank(grain) >= _grain_rank("day") else "day"
        return not moment.utcoffset() and _is_grain_boundary(moment, grain)
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
    return ""


def _aggregate_relation_rejection_reason(
    row: AggregateRelationConfig,
    *,
    bound: BoundMeasure,
    query: NormalizedQuery,
    leaf_time_role: str,
    required_dimensions: set[str],
    counts_fact_key: bool,
) -> str:
    if row.source != "default":
        return "non_default_source"
    if (row.equivalence_kind or "exact") != "exact":
        return "non_exact_equivalence"
    if row.filters:
        # The rollup holds only the rows its filters kept, and no query is proven to imply them.
        return "rollup_filter_not_implied"
    time = query.time
    requested_grain = str((time.grain if time else "") or "").lower()
    if time is None or not requested_grain:
        return "missing_query_time_grain"
    if row.temporal_role and row.temporal_role != leaf_time_role:
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
    if bound.measure_id not in _aggregate_measure_coverage(row):
        return "missing_measure"
    if bound.measure_id not in row.measure_columns:
        return "missing_measure_column"
    if required_dimensions - _aggregate_dimension_coverage(row):
        return "missing_dimension"
    rollup = str(row.measure_rollups.get(bound.measure_id, "") or "").lower()
    if rollup not in {"additive", "precomputed"}:
        return "unsupported_rollup"
    aggregation = str(row.measure_aggregations.get(bound.measure_id, "") or "").lower()
    if aggregation and aggregation != "sum":
        return "unsupported_rollup_aggregation"
    query_aggregation = str(bound.aggregation or "").lower()
    if query_aggregation not in {"sum", "count", "count_distinct"}:
        return "unsupported_query_aggregation"
    if query_aggregation == "count_distinct" and not counts_fact_key:
        # Distinct counts can't be added up: one key can sit in several rollup rows.
        return "aggregation_not_reaggregable"
    return ""


def _select_aggregate_relation(
    *,
    bound: BoundMeasure,
    query: NormalizedQuery,
    config: PackageConfig,
) -> tuple[str, dict[str, str]]:
    """Pick the rollup that answers a measure leaf exactly, and say why each other one can't."""
    measure = _measure_index(config)[bound.measure_id]
    rows = [row for row in config.aggregate_relations if row.source_entity == measure.entity]
    if not rows:
        return "", {}
    leaf_time_role = _leaf_time_role(bound, query, config)
    required_dimensions = {str(item) for item in query.group_by if str(item)}
    required_dimensions.update(str(item.field) for item in query.where if str(item.field))
    required_dimensions.update(
        field
        for field in (str(item.get("field", "")) for item in _bound_filter_clauses(bound, config))
        if field
    )
    key = _measure_count_distinct_key_columns(measure, config)
    # Each value of a model's single-column row key sits in one rollup row; other keys can repeat.
    counts_fact_key = len(key) == 1 and measure.row_grain == key and not measure.source_relation
    blocker = _leaf_rollup_blocker(bound, query, config, leaf_time_role)
    rejections = {
        row.id: reason
        for row in rows
        if (
            reason := blocker
            or _aggregate_relation_rejection_reason(
                row,
                bound=bound,
                query=query,
                leaf_time_role=leaf_time_role,
                required_dimensions=required_dimensions,
                counts_fact_key=counts_fact_key,
            )
        )
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
