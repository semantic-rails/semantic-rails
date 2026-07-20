"""Recovery enrichment for MIXED_GRAIN_INVALID measure/dimension pairings.

When the planner refuses a measure-dimension pairing because the join
path requires an unsupported rewrite, the bare error is a dead end for
agents: a blind-agent evaluation showed five failed calls of brute
force before discovering that ``measure.jaffle.item_revenue_usd`` works
with product dimensions where ``measure.jaffle.revenue_usd`` does not.

This module computes — from registry metadata only, never the
warehouse — the concrete escape routes:

1. ``compatible_measures``: measures that CAN group by the offending
   dimension (same entity, direct key projection, or a relationship
   path whose fan-out analysis is clean), ranked so semantically
   similar names come first.
2. ``compatible_dimensions``: dimensions that work with the requested
   measure, ranked by similarity to the offending dimension.
3. ``time_axis_recovery``: when the offending dimension lives on a
   calendar/time entity (e.g. ``dimension.jaffle_time_month_start``),
   the right move is a ``time`` block with ``time.grain`` — not a
   calendar-date group_by. The suggested temporal role and grain are
   derived from the query and the dimension name.

The computation is bounded: candidate scans are capped and each
pairing check is a cached path lookup plus a fan-out analysis, so
enrichment stays cheap even for large registries. Failures inside the
enrichment never mask the original error — the caller gets an empty
enrichment instead.
"""

from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any

from ..ast import NormalizedQuery
from ..errors import SemanticLayerError
from ..fanout import analyze_fanout, choose_path, package_hop_limit
from ..schema import DimensionConfig, MeasureConfig, PackageConfig
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _temporal_role_index,
    get_package_analysis,
)
from .paths import _direct_dimension_source_expr
from .temporal import _time_bound_relationship_ids

# Bounded enrichment: at most this many suggestions per list, and at
# most this many candidates ever run through the pairing check.
_MAX_SUGGESTIONS = 5
_MAX_CANDIDATES_SCANNED = 80

# Calendar-dimension name tokens mapped to the time.grain they imply
# (dimension.jaffle_time_month_start -> "month"). Ordered longest
# token first so "quarter_start" wins over a bare "quarter" scan.
_GRAIN_TOKENS: tuple[tuple[str, str], ...] = (
    ("quarter", "quarter"),
    ("month", "month"),
    ("week", "week"),
    ("year", "year"),
    ("day", "day"),
    ("date", "day"),
)

_DATE_DATA_TYPES = frozenset({"date", "timestamp"})
_QUERY_OPTIONAL_FIELDS = (
    "group_by",
    "where",
    "metric_filters",
    "time",
    "temporal_role_overrides",
    "order_by",
    "limit",
)


def _object_tail(object_id: str) -> str:
    return object_id.rsplit(".", 1)[-1]


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _object_tail(left), _object_tail(right)).ratio()


def _chosen_path(
    config: PackageConfig, *, start: str, target: str, preference: str
) -> list[str] | None:
    """Cheapest registry path between two entities, or None.

    Mirrors the compiler's path selection (explicit path preferences
    first, then the cached BFS chooser) but swallows PATH_NOT_FOUND /
    AMBIGUOUS_PATH — for enrichment an unreachable pairing is simply
    incompatible, not an error.
    """
    explicit = get_package_analysis(config).path_preferences.get((start, target))
    if explicit is not None:
        return list(explicit)
    try:
        chosen, _candidates = choose_path(
            config,
            start=start,
            target=target,
            hop_limit=package_hop_limit(config),
            preference=preference,
        )
    except SemanticLayerError:
        return None
    return list(chosen)


def _pairing_is_clean(
    config: PackageConfig,
    measure: MeasureConfig,
    dim: DimensionConfig,
    *,
    preference: str,
    time_bound_relationships: set[str],
) -> bool:
    """True when `measure` can group by `dim` without a grain rewrite."""
    if not dim.groupable:
        return False
    if measure.entity == dim.entity:
        return True
    try:
        if _direct_dimension_source_expr(measure.entity, dim.id, config) is not None:
            return True
    except SemanticLayerError:
        return False
    path = _chosen_path(config, start=measure.entity, target=dim.entity, preference=preference)
    if not path:
        return False
    analysis = analyze_fanout(
        config, measure.entity, path, time_bound_relationships=time_bound_relationships
    )
    return analysis.get("status") == "ok"


def _offending_dimension_ids(
    config: PackageConfig, query: NormalizedQuery, target_entity: str
) -> list[str]:
    """Query dimensions (group_by / where / time role) on the offending entity."""
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    offending: list[str] = []
    for dim_id in query.group_by:
        dim = dimensions.get(dim_id)
        if dim is not None and dim.entity == target_entity:
            offending.append(dim_id)
    for item in query.where:
        dim = dimensions.get(item.field)
        if dim is not None and dim.entity == target_entity:
            offending.append(item.field)
    if query.time is not None:
        role = temporal_roles.get(query.time.temporal_role)
        if role is not None:
            dim = dimensions.get(role.dimension)
            if dim is not None and dim.entity == target_entity:
                offending.append(dim.id)
    return list(dict.fromkeys(offending))


def _is_calendar_dimension(config: PackageConfig, dim: DimensionConfig) -> bool:
    entity = _entity_index(config).get(dim.entity)
    if entity is not None and (entity.kind == "time" or entity.calendar_id):
        return True
    return dim.data_type in _DATE_DATA_TYPES


def _suggested_grain(dim_id: str) -> str:
    tail = _object_tail(dim_id).lower()
    for token, grain in _GRAIN_TOKENS:
        if token in tail:
            return grain
    return ""


def _suggested_temporal_role(
    config: PackageConfig, query: NormalizedQuery, measure: MeasureConfig | None
) -> str:
    if query.time is not None and query.time.temporal_role:
        return query.time.temporal_role
    if measure is not None:
        if measure.default_temporal_role:
            return measure.default_temporal_role
        if measure.compatible_temporal_roles:
            return measure.compatible_temporal_roles[0]
    for role in config.temporal_roles:
        if role.default_query_time_axis:
            return role.id
    return ""


def _compact_query_payload(query: NormalizedQuery) -> dict[str, Any]:
    raw = query.to_dict()
    payload: dict[str, Any] = {
        "version": raw.get("version", 1),
        "select": deepcopy(raw.get("select", []) or []),
    }
    for field in _QUERY_OPTIONAL_FIELDS:
        value = raw.get(field)
        if value not in (None, [], {}):
            payload[field] = deepcopy(value)
    return payload


def _replace_measure_refs(
    value: Any,
    *,
    requested: set[str],
    replacement: str,
    replacement_aggregation: str,
) -> tuple[Any, bool]:
    if isinstance(value, list):
        replaced = False
        list_out = []
        for item in value:
            new_item, item_replaced = _replace_measure_refs(
                item,
                requested=requested,
                replacement=replacement,
                replacement_aggregation=replacement_aggregation,
            )
            list_out.append(new_item)
            replaced = replaced or item_replaced
        return list_out, replaced
    if not isinstance(value, dict):
        return deepcopy(value), False
    dict_out: dict[str, Any] = {}
    replaced = False
    replace_current = value.get("measure") in requested
    for key, item in value.items():
        if key == "measure" and replace_current:
            dict_out[key] = replacement
            replaced = True
            continue
        if key == "aggregation" and replace_current and replacement_aggregation:
            dict_out[key] = replacement_aggregation
            continue
        new_item, item_replaced = _replace_measure_refs(
            item,
            requested=requested,
            replacement=replacement,
            replacement_aggregation=replacement_aggregation,
        )
        dict_out[key] = new_item
        replaced = replaced or item_replaced
    return dict_out, replaced


def _query_replacing_measure(
    query: NormalizedQuery,
    *,
    requested: list[str],
    replacement: str,
    replacement_aggregation: str,
) -> dict[str, Any]:
    requested_set = set(requested)
    if not requested_set or not replacement:
        return {}
    payload = _compact_query_payload(query)
    replaced = False
    select: list[dict[str, Any]] = []
    for item in payload.get("select", []) or []:
        row = dict(item)
        expression, item_replaced = _replace_measure_refs(
            row.get("expression", {}),
            requested=requested_set,
            replacement=replacement,
            replacement_aggregation=replacement_aggregation,
        )
        row["expression"] = expression
        select.append(row)
        replaced = replaced or item_replaced
    if not replaced:
        return {}
    payload["select"] = select
    return payload


def _query_replacing_dimension(
    query: NormalizedQuery, *, offending_dim_ids: list[str], replacement: str
) -> dict[str, Any]:
    offending = set(offending_dim_ids)
    if not offending or not replacement:
        return {}
    payload = _compact_query_payload(query)
    group_by = [
        replacement if dim_id in offending else dim_id
        for dim_id in list(payload.get("group_by", []) or [])
    ]
    if group_by == list(payload.get("group_by", []) or []):
        return {}
    payload["group_by"] = list(dict.fromkeys(group_by))
    return payload


def _query_using_time_axis(
    query: NormalizedQuery, *, calendar_dimension: str, temporal_role: str, grain: str
) -> dict[str, Any]:
    if not calendar_dimension or not temporal_role or not grain:
        return {}
    payload = _compact_query_payload(query)
    group_by = [
        dim_id for dim_id in list(payload.get("group_by", []) or []) if dim_id != calendar_dimension
    ]
    if group_by:
        payload["group_by"] = group_by
    else:
        payload.pop("group_by", None)
    time = dict(payload.get("time", {}) or {})
    time["temporal_role"] = temporal_role
    time["grain"] = grain
    payload["time"] = time
    return payload


def _compatible_measures(
    config: PackageConfig,
    *,
    offending_dims: list[DimensionConfig],
    requested_measure_ids: set[str],
    anchor_measure_id: str,
    preference: str,
    time_bound_relationships: set[str],
) -> list[str]:
    candidates = sorted(
        (m for m in config.measures if m.id not in requested_measure_ids),
        key=lambda m: _similarity(m.id, anchor_measure_id),
        reverse=True,
    )
    compatible: list[str] = []
    for measure in candidates[:_MAX_CANDIDATES_SCANNED]:
        if all(
            _pairing_is_clean(
                config,
                measure,
                dim,
                preference=preference,
                time_bound_relationships=time_bound_relationships,
            )
            for dim in offending_dims
        ):
            compatible.append(measure.id)
            if len(compatible) >= _MAX_SUGGESTIONS:
                break
    return compatible


def _compatible_dimensions(
    config: PackageConfig,
    *,
    anchor_measure: MeasureConfig,
    offending_dim_ids: set[str],
    anchor_dim_id: str,
    preference: str,
    time_bound_relationships: set[str],
) -> list[str]:
    candidates = sorted(
        (d for d in config.dimensions if d.id not in offending_dim_ids and d.groupable),
        key=lambda d: _similarity(d.id, anchor_dim_id),
        reverse=True,
    )
    compatible: list[str] = []
    for dim in candidates[:_MAX_CANDIDATES_SCANNED]:
        if _pairing_is_clean(
            config,
            anchor_measure,
            dim,
            preference=preference,
            time_bound_relationships=time_bound_relationships,
        ):
            compatible.append(dim.id)
            if len(compatible) >= _MAX_SUGGESTIONS:
                break
    return compatible


def mixed_grain_pairing_enrichment(
    *,
    config: PackageConfig,
    query: NormalizedQuery,
    measure_ids: list[str],
    target_entity: str,
) -> dict[str, Any]:
    """Details enrichment for a MIXED_GRAIN_INVALID measure/dimension pairing.

    Returns a (possibly empty) dict safe to merge into the error
    ``details``. Registry metadata only — no warehouse access.
    """
    try:
        return _enrichment_unsafe(
            config=config, query=query, measure_ids=measure_ids, target_entity=target_entity
        )
    except Exception:  # noqa: BLE001 — enrichment must never mask the original error
        return {}


def _enrichment_unsafe(
    *,
    config: PackageConfig,
    query: NormalizedQuery,
    measure_ids: list[str],
    target_entity: str,
) -> dict[str, Any]:
    measures = _measure_index(config)
    dimensions = _dimension_index(config)
    requested = [mid for mid in dict.fromkeys(measure_ids) if mid in measures]
    if not requested:
        return {}
    anchor_measure = measures[requested[0]]

    offending_dim_ids = _offending_dimension_ids(config, query, target_entity)
    offending_dims = [dimensions[dim_id] for dim_id in offending_dim_ids]
    preference = query.path_policy.preference
    time_bound = _time_bound_relationship_ids(query, config)

    enrichment: dict[str, Any] = {"measures": requested}
    if offending_dim_ids:
        enrichment["offending_dimensions"] = offending_dim_ids

    if offending_dims:
        compatible_measures = _compatible_measures(
            config,
            offending_dims=offending_dims,
            requested_measure_ids=set(requested),
            anchor_measure_id=anchor_measure.id,
            preference=preference,
            time_bound_relationships=time_bound,
        )
        if compatible_measures:
            enrichment["compatible_measures"] = compatible_measures
            enrichment["closest_compatible_measure"] = compatible_measures[0]
            query_template = _query_replacing_measure(
                query,
                requested=requested,
                replacement=compatible_measures[0],
                replacement_aggregation=measures[compatible_measures[0]].default_aggregation,
            )
            if query_template:
                enrichment["closest_compatible_measure_query"] = query_template

    compatible_dimensions = _compatible_dimensions(
        config,
        anchor_measure=anchor_measure,
        offending_dim_ids=set(offending_dim_ids),
        anchor_dim_id=offending_dim_ids[0] if offending_dim_ids else anchor_measure.id,
        preference=preference,
        time_bound_relationships=time_bound,
    )
    if compatible_dimensions:
        enrichment["compatible_dimensions"] = compatible_dimensions
        query_template = _query_replacing_dimension(
            query, offending_dim_ids=offending_dim_ids, replacement=compatible_dimensions[0]
        )
        if query_template:
            enrichment["closest_compatible_dimension_query"] = query_template

    calendar_dims = [dim for dim in offending_dims if _is_calendar_dimension(config, dim)]
    if calendar_dims:
        calendar_dim = calendar_dims[0]
        recovery: dict[str, Any] = {"calendar_dimension": calendar_dim.id}
        grain = _suggested_grain(calendar_dim.id)
        if grain:
            recovery["grain"] = grain
        role = _suggested_temporal_role(config, query, anchor_measure)
        if role:
            recovery["temporal_role"] = role
        query_template = _query_using_time_axis(
            query,
            calendar_dimension=calendar_dim.id,
            temporal_role=role,
            grain=grain,
        )
        if query_template:
            recovery["closest_valid_query"] = query_template
        enrichment["time_axis_recovery"] = recovery

    return enrichment
