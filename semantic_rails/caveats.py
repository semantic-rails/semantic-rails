"""Package-authored caveat matching.

Caveats are advisory package metadata. They never affect planning,
SQL lowering, access policy, or result rows; this module only decides
which caveats are relevant enough to surface as structured warnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .diagnostics import semantic_issue
from .expressions import expr_to_dict
from .policies import context_scope_matches
from .schema import PackageConfig, SemanticCaveatConfig


@dataclass(frozen=True)
class _Interval:
    start: str = ""
    end: str = ""
    source: str = "query"


@dataclass
class _Signals:
    strong_object_ids: set[str] = field(default_factory=set)
    strong_entity_ids: set[str] = field(default_factory=set)
    exposed_dimensions: set[str] = field(default_factory=set)
    filtered_values_by_dimension: dict[str, set[str]] = field(default_factory=dict)
    intervals: list[_Interval] = field(default_factory=list)
    comparative: bool = False
    time_grained: bool = False
    group_by: list[str] = field(default_factory=list)


@dataclass
class _Match:
    caveat: SemanticCaveatConfig
    reasons: list[str]
    matched_objects: list[str]
    matched_time: dict[str, Any]
    matched_entity_values: list[dict[str, Any]]
    score: int


_SEVERITY_RANK = {"warning": 2, "info": 1}
_VALID_GRAINS = {"day", "week", "month", "quarter", "year"}


def caveat_warnings(
    config: PackageConfig,
    compiled: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return structured warnings for caveats relevant to a compiled query."""
    caveats = list(getattr(config, "semantic_caveats", []) or [])
    if not caveats:
        return []
    payload = dict(payload or {})
    verbosity = str(payload.get("verbosity", "") or "compact").strip().lower()
    if verbosity not in {"minimal", "compact", "full"}:
        verbosity = "compact"
    signals = _signals_for_query(config, compiled, payload)
    policy_context = dict(payload.get("policy_context", {}) or {})
    matches: list[_Match] = []
    for caveat in caveats:
        match = _match_caveat(caveat, config, signals, policy_context)
        if match is not None:
            matches.append(match)
    if not matches:
        return []

    matches.sort(
        key=lambda row: (
            -_SEVERITY_RANK.get(row.caveat.severity, 0),
            -row.score,
            row.caveat.id,
        )
    )
    cap = {"minimal": 3, "compact": 5}.get(verbosity, len(matches))
    selected = matches[:cap]
    warnings = [_warning_for_match(match, verbosity=verbosity) for match in selected]
    omitted = len(matches) - len(selected)
    if omitted > 0:
        warnings.append(
            semantic_issue(
                code="SEMANTIC_CAVEATS_TRUNCATED",
                message="Additional semantic caveats matched but were omitted at this verbosity.",
                severity="info",
                stage="planning",
                details={"omitted_count": omitted, "matched_count": len(matches)},
            )
        )
    return warnings


def _warning_for_match(match: _Match, *, verbosity: str) -> dict[str, Any]:
    caveat = match.caveat
    details: dict[str, Any] = {
        "kind": caveat.kind,
        "caveat_id": caveat.id,
        "match_reasons": list(match.reasons),
        "agent_action": "mention_when_interpreting_results",
    }
    if verbosity in {"compact", "full"}:
        details.update(
            {
                "matched_objects": list(match.matched_objects),
                "matched_time": dict(match.matched_time),
                "matched_entity_values": list(match.matched_entity_values),
            }
        )
    if verbosity == "full":
        details.update(
            {
                "owner": caveat.owner,
                "references": list(caveat.references),
            }
        )
    return semantic_issue(
        code="SEMANTIC_CAVEAT_APPLIED",
        message=caveat.message,
        severity=caveat.severity,
        stage="planning",
        details=details,
        object_ids=caveat.object_ids,
    )


def _signals_for_query(
    config: PackageConfig, compiled: dict[str, Any], payload: dict[str, Any]
) -> _Signals:
    plan = compiled["logical_plan"]
    query = dict(
        getattr(plan, "query", {})
        or getattr(compiled.get("explain"), "normalized_query", None)
        or {}
    )
    signals = _Signals()
    dimensions = {row.id: row for row in config.dimensions}
    metrics = {row.id: row for row in config.metric_recipes}

    signals.group_by = [str(item) for item in list(query.get("group_by", []) or [])]
    signals.exposed_dimensions.update(signals.group_by)
    signals.strong_object_ids.update(signals.group_by)
    for dim_id in signals.group_by:
        dim = dimensions.get(dim_id)
        if dim is not None:
            signals.strong_entity_ids.add(dim.entity)

    for row in list(query.get("where", []) or []):
        if not isinstance(row, dict):
            continue
        field = str(row.get("field", "") or "")
        if field:
            signals.strong_object_ids.add(field)
            dim = dimensions.get(field)
            if dim is not None:
                signals.strong_entity_ids.add(dim.entity)
        values = _where_values(row)
        if field and values:
            signals.filtered_values_by_dimension.setdefault(field, set()).update(values)

    time_spec = dict(getattr(plan, "time", {}) or query.get("time", {}) or {})
    temporal_role = str(time_spec.get("temporal_role", "") or "")
    if temporal_role:
        signals.strong_object_ids.add(temporal_role)
    signals.time_grained = bool(str(time_spec.get("grain", "") or ""))

    for item in list(query.get("select", []) or []):
        if not isinstance(item, dict):
            continue
        expr = dict(item.get("expression", {}) or {})
        signals.strong_object_ids.update(_collect_expr_object_ids(expr))
        if str(expr.get("kind", "") or "") == "prior_period":
            signals.comparative = True

    for item in list(query.get("metric_filters", []) or []):
        if isinstance(item, dict):
            signals.strong_object_ids.update(
                _collect_expr_object_ids(dict(item.get("expression", {}) or {}))
            )

    selected_metric_ids = [
        object_id for object_id in signals.strong_object_ids if object_id in metrics
    ]
    for metric_id in selected_metric_ids:
        metric = metrics[metric_id]
        expr_payload = expr_to_dict(metric.expression)
        signals.strong_object_ids.update(_collect_expr_object_ids(expr_payload))
        if _contains_kind(expr_payload, "prior_period"):
            signals.comparative = True
        if metric.temporal_role:
            signals.strong_object_ids.add(metric.temporal_role)

    for bound in list(getattr(plan, "bound_measures", []) or []):
        measure_id = str(getattr(bound, "measure_id", "") or "")
        if measure_id:
            signals.strong_object_ids.add(measure_id)

    for path in dict(getattr(plan, "selected_paths", {}) or {}).values():
        rel_ids = [str(rel_id) for rel_id in list(path or [])]
        signals.strong_object_ids.update(rel_ids)

    signals.intervals = _query_intervals(query, signals.comparative)
    return signals


def _where_values(row: dict[str, Any]) -> set[str]:
    op = str(row.get("op", "") or "").strip().lower()
    if op not in {"=", "==", "eq", "in"}:
        return set()
    raw = row.get("value")
    if isinstance(raw, list):
        return {str(value) for value in raw}
    if raw is None:
        return set()
    return {str(raw)}


def _collect_expr_object_ids(expr: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    if not isinstance(expr, dict):
        return ids
    if expr.get("measure"):
        ids.add(str(expr["measure"]))
    if expr.get("metric"):
        ids.add(str(expr["metric"]))
    for child in _expr_children(expr):
        ids.update(_collect_expr_object_ids(child))
    return ids


def _contains_kind(expr: dict[str, Any], kind: str) -> bool:
    if not isinstance(expr, dict):
        return False
    if str(expr.get("kind", "") or "") == kind:
        return True
    return any(_contains_kind(child, kind) for child in _expr_children(expr))


def _expr_children(expr: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for key in (
        "left",
        "right",
        "input",
        "numerator",
        "denominator",
        "over",
        "base",
        "converted",
        "value",
        "null_value",
    ):
        child = expr.get(key)
        if isinstance(child, dict):
            children.append(child)
    for key in ("args",):
        for child in list(expr.get(key, []) or []):
            if isinstance(child, dict):
                children.append(child)
    for item in list(expr.get("whens", []) or []):
        if isinstance(item, dict):
            for key in ("when", "then"):
                child = item.get(key)
                if isinstance(child, dict):
                    children.append(child)
    return children


def _query_intervals(query: dict[str, Any], comparative: bool) -> list[_Interval]:
    time_spec = dict(query.get("time", {}) or {})
    start = _date_key(time_spec.get("start"))
    end = _date_key(time_spec.get("end"))
    grain = str(time_spec.get("grain", "") or "").strip().lower()
    intervals: list[_Interval] = []
    base = _base_interval(start=start, end=end, grain=grain)
    if base is not None:
        intervals.append(base)

    prior_nodes = _prior_period_nodes(query)
    if prior_nodes:
        comparative = True
    if base is not None:
        for node in prior_nodes:
            shifted = _prior_interval(base, node)
            if shifted is not None:
                intervals.append(shifted)

    deduped: dict[tuple[str, str, str], _Interval] = {}
    for interval in intervals:
        deduped[(interval.start, interval.end, interval.source)] = interval
    return list(deduped.values()) if comparative or intervals else []


def _base_interval(*, start: str, end: str, grain: str) -> _Interval | None:
    if start or end:
        if start or not (end and grain in _VALID_GRAINS):
            return _Interval(start=start, end=end, source="query")
        inferred = _shift_iso(end, -1, grain)
        return (
            _Interval(start=inferred, end=end, source="query_inferred_grain") if inferred else None
        )
    return None


def _prior_period_nodes(query: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for key in ("select", "metric_filters", "order_by"):
        for row in list(query.get(key, []) or []):
            if not isinstance(row, dict):
                continue
            expr = dict(row.get("expression", {}) or {})
            nodes.extend(_walk_kind(expr, "prior_period"))
    return nodes


def _walk_kind(expr: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if not isinstance(expr, dict):
        return []
    rows = [expr] if str(expr.get("kind", "") or "") == kind else []
    for child in _expr_children(expr):
        rows.extend(_walk_kind(child, kind))
    return rows


def _prior_interval(base: _Interval, node: dict[str, Any]) -> _Interval | None:
    offset = node.get("offset", -1)
    unit = str(node.get("grain", "") or "").strip().lower()
    value = -1
    if isinstance(offset, dict):
        unit = str(offset.get("unit", unit) or unit).strip().lower()
        try:
            value = -abs(int(offset.get("value", 1) or 1))
        except (TypeError, ValueError):
            value = -1
    else:
        try:
            value = int(offset)
        except (TypeError, ValueError):
            value = -1
        if value > 0:
            value = -value
    if unit not in _VALID_GRAINS:
        return None
    start = _shift_iso(base.start, value, unit) if base.start else ""
    end = _shift_iso(base.end, value, unit) if base.end else ""
    if not start and not end:
        return None
    return _Interval(start=start, end=end, source="comparison_prior_period")


def _match_caveat(
    caveat: SemanticCaveatConfig,
    config: PackageConfig,
    signals: _Signals,
    policy_context: dict[str, Any],
) -> _Match | None:
    if not context_scope_matches(caveat.audiences, str(policy_context.get("audience", "") or "")):
        return None
    if not context_scope_matches(
        caveat.environments, str(policy_context.get("environment", "") or "")
    ):
        return None

    entity_ids = {row.id for row in config.entities}
    reasons: list[str] = []
    score = 0
    matched_objects: list[str] = []

    object_ids = set(caveat.object_ids)
    non_entity_matches = sorted((object_ids - entity_ids) & signals.strong_object_ids)
    entity_matches = sorted((object_ids & entity_ids) & signals.strong_entity_ids)
    if non_entity_matches:
        reasons.append("semantic_object")
        score += 3
        matched_objects.extend(non_entity_matches)
    if entity_matches:
        reasons.append("entity_exposed")
        score += 2
        matched_objects.extend(entity_matches)
    matched_entity_values = _matched_entity_values(caveat, signals)
    if caveat.entity_values and not matched_entity_values:
        return None
    if matched_entity_values:
        reasons.append("entity_value")
        score += 4
        for row in matched_entity_values:
            dimension = str(row.get("dimension", "") or "")
            entity = str(row.get("entity", "") or "")
            if dimension:
                matched_objects.append(dimension)
            if entity:
                matched_objects.append(entity)
    if not non_entity_matches and not entity_matches and not matched_entity_values:
        # Entity matching only counts entities the query exposes through a
        # grouped or filtered dimension. Entities merely involved via measure
        # internals or the join plan are intentionally ignored: involvement
        # proves the entity participates, not that the caveat is useful.
        return None

    time_match = _match_time(caveat, signals, bool(matched_entity_values))
    if time_match is None:
        return None
    if time_match:
        reasons.extend(time_match.get("reasons", []))
        score += int(time_match.get("score", 0) or 0)

    return _Match(
        caveat=caveat,
        reasons=list(dict.fromkeys(reasons)),
        matched_objects=list(dict.fromkeys(matched_objects)),
        matched_time=dict(time_match or {}),
        matched_entity_values=matched_entity_values,
        score=score,
    )


def _matched_entity_values(caveat: SemanticCaveatConfig, signals: _Signals) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in caveat.entity_values:
        dimension = str(row.get("dimension", "") or "")
        entity = str(row.get("entity", "") or "")
        values = _entity_value_set(row)
        filtered = signals.filtered_values_by_dimension.get(dimension, set())
        if filtered and values and filtered.isdisjoint(values):
            continue
        if filtered and (not values or not filtered.isdisjoint(values)):
            matches.append(
                {
                    "entity": entity,
                    "dimension": dimension,
                    "values": sorted(values or filtered),
                    "match": "filtered",
                }
            )
            continue
        if dimension in signals.exposed_dimensions:
            matches.append(
                {
                    "entity": entity,
                    "dimension": dimension,
                    "values": sorted(values),
                    "match": "exposed",
                }
            )
    return matches


def _entity_value_set(row: dict[str, Any]) -> set[str]:
    if isinstance(row.get("values"), list):
        return {str(value) for value in row.get("values", [])}
    if "value" in row:
        return {str(row.get("value"))}
    return set()


def _match_time(
    caveat: SemanticCaveatConfig, signals: _Signals, has_entity_value_match: bool
) -> dict[str, Any] | None:
    time_spec = dict(caveat.time or {})
    if not time_spec:
        return {}
    if not signals.intervals:
        return None
    if time_spec.get("at"):
        return _match_point_time(time_spec["at"], signals, has_entity_value_match)
    return _match_range_time(time_spec, signals, has_entity_value_match)


def _match_point_time(
    point: str, signals: _Signals, has_entity_value_match: bool
) -> dict[str, Any] | None:
    contained = [interval for interval in signals.intervals if _contains_point(interval, point)]
    crossed = _bookends_point(signals.intervals, point)
    if not contained and not crossed:
        return None
    if contained and _broad_scalar_suppressed(
        signals, contained[0], point=point, has_entity_value_match=has_entity_value_match
    ):
        return None
    reasons = ["time_point_crossed" if crossed and not contained else "time_point_overlap"]
    return {
        "at": point,
        "intervals": [_interval_payload(row) for row in (contained or signals.intervals)],
        "reasons": reasons,
        "score": 3 if crossed else 2,
    }


def _match_range_time(
    time_spec: dict[str, str], signals: _Signals, has_entity_value_match: bool
) -> dict[str, Any] | None:
    window_start = time_spec.get("from", "")
    window_end = time_spec.get("to", "")
    overlapped = [
        interval
        for interval in signals.intervals
        if _range_intersects(interval.start, interval.end, window_start, window_end)
    ]
    bookended = _bookends_range(signals.intervals, window_start, window_end)
    if not overlapped and not bookended:
        return None
    if overlapped and _broad_scalar_suppressed(
        signals,
        overlapped[0],
        window_start=window_start,
        window_end=window_end,
        has_entity_value_match=has_entity_value_match,
    ):
        return None
    reasons = ["time_range_bookended" if bookended and not overlapped else "time_range_overlap"]
    return {
        "from": window_start,
        "to": window_end,
        "intervals": [_interval_payload(row) for row in (overlapped or signals.intervals)],
        "reasons": reasons,
        "score": 3 if bookended else 2,
    }


def _broad_scalar_suppressed(
    signals: _Signals,
    interval: _Interval,
    *,
    point: str = "",
    window_start: str = "",
    window_end: str = "",
    has_entity_value_match: bool,
) -> bool:
    if signals.time_grained or signals.comparative or signals.group_by or has_entity_value_match:
        return False
    start = _parse_date(interval.start)
    end = _parse_date(interval.end)
    if start is None or end is None or start >= end:
        return False
    query_days = max((end - start).days, 1)
    if point:
        return query_days >= 90
    c_start = _parse_date(window_start)
    c_end = _parse_date(window_end)
    if c_start is None or c_end is None or c_start >= c_end:
        return False
    overlap_start = max(start, c_start)
    overlap_end = min(end, c_end)
    overlap_days = max((overlap_end - overlap_start).days, 0)
    return overlap_days > 0 and (overlap_days / query_days) < 0.25


def _contains_point(interval: _Interval, point: str) -> bool:
    if interval.start and interval.start > point:
        return False
    return not (interval.end and interval.end <= point)


def _bookends_point(intervals: list[_Interval], point: str) -> bool:
    before = any(interval.end and interval.end <= point for interval in intervals)
    after = any(interval.start and interval.start >= point for interval in intervals)
    return before and after


def _range_intersects(start: str, end: str, window_start: str, window_end: str) -> bool:
    lower_ok = not window_end or not start or start < window_end
    upper_ok = not window_start or not end or end > window_start
    return lower_ok and upper_ok


def _bookends_range(intervals: list[_Interval], start: str, end: str) -> bool:
    if not start or not end:
        return False
    before = any(interval.end and interval.end <= start for interval in intervals)
    after = any(interval.start and interval.start >= end for interval in intervals)
    return before and after


def _interval_payload(interval: _Interval) -> dict[str, str]:
    return {"start": interval.start, "end": interval.end, "source": interval.source}


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0].split(" ", 1)[0]


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(_date_key(value))
    except ValueError:
        return None


def _shift_iso(value: str, amount: int, unit: str) -> str:
    current = _parse_date(value)
    if current is None or unit not in _VALID_GRAINS:
        return ""
    if unit == "day":
        return (current + timedelta(days=amount)).isoformat()
    if unit == "week":
        return (current + timedelta(weeks=amount)).isoformat()
    months = amount
    if unit == "quarter":
        months *= 3
    elif unit == "year":
        months *= 12
    total = current.year * 12 + (current.month - 1) + months
    year = total // 12
    month = (total % 12) + 1
    day = min(current.day, _last_day(year, month))
    return date(year, month, day).isoformat()


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day
