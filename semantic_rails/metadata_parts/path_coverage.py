"""Reachability and history-coverage answers for catalog objects.

Discover, inspect, and valid-values all need the same set of questions
answered about a given object:

* "From the current root entity, can I get to this dimension / measure /
  entity at all, and on what relationship path?" — ``_path_availability``,
  and ``_path_reach`` for its cached answer.
* "Does any relationship on that path carry temporal validity, so my
  result will be history-aware (and possibly include NULL buckets)?" —
  ``_path_has_temporal_validity``.
* "Should I attach a NULL-coverage caveat to the response?" —
  ``_history_coverage_note`` and its two callers
  ``_dimension_history_coverage`` / ``_metric_history_coverage_notes``.
* "What declared value domain (if any) covers this dimension, and what
  rows can I show without hitting the warehouse?" —
  ``_value_domain_for_dimension`` / ``_declared_value_rows`` /
  ``_valid_values_lookup_hint``.

``_filter_value_rows`` lives here because the value-row pagination it
provides is only used inside this domain (declared value rows during
catalog inspect, and the same shape during a valid-values lookup).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from typing import Any, NamedTuple

from ..catalog_search import normalize_search_value
from ..compiler_parts.indexes import get_package_analysis
from ..errors import SemanticLayerError
from ..fanout import analyze_fanout, query_routes_active, resolve_path, route_label
from ..schema import PackageConfig


class PathReach(NamedTuple):
    """The part of ``_path_availability`` a catalog row reads: whether the root entity reaches
    the target, why not, and on which route. Immutable, so one cached answer serves every
    caller."""

    available: bool
    reason: str
    path: tuple[str, ...]


_SAME_ENTITY = PathReach(True, "", ())


def _resolved_availability(
    config: PackageConfig, root_entity: str, target_entity: str
) -> dict[str, Any]:
    """``_path_availability`` of a pair whose route resolves; raises the pair's refusal."""
    path, candidates = resolve_path(config, start=root_entity, target=target_entity)
    analysis = analyze_fanout(config, root_entity, path, validity_windows=False)
    status = analysis["status"]
    # A labelled package decision for the pair says which meaning the path is.
    label = route_label(config, root_entity, target_entity, path)
    return {
        "available": status == "ok",
        "reason": "" if status == "ok" else "requires leaf rewrite or grain-aware plan",
        "path": path,
        "candidates": candidates,
        "analysis": analysis,
        **({"route_label": label} if label else {}),
    }


def _refusal_reason(exc: SemanticLayerError) -> str:
    # The catalog repeats this per object; the routes and their rows are in details.
    return "route decision required" if exc.code == "AMBIGUOUS_PATH" else str(exc)


def _path_availability(
    config: PackageConfig, root_entity: str, target_entity: str
) -> dict[str, Any]:
    if not root_entity or target_entity == root_entity:
        return {"available": True, "reason": "", "path": [], "candidates": []}
    try:
        return _resolved_availability(config, root_entity, target_entity)
    except SemanticLayerError as exc:
        return {
            "available": False,
            "reason": _refusal_reason(exc),
            "path": [],
            "candidates": [],
            "error_code": exc.code,
            "details": exc.details,
        }


def _path_reach(config: PackageConfig, root_entity: str, target_entity: str) -> PathReach:
    """``_path_availability``'s availability, reason and path, cached per package on the
    package's analysis, so a reloaded package or a caller's view starts empty. A query's own
    route decisions answer for that query only, so nothing is cached while they are in force.
    """
    if not root_entity or target_entity == root_entity:
        return _SAME_ENTITY
    pair = (root_entity, target_entity)
    cache = None if query_routes_active() else get_package_analysis(config).path_reach
    reach = cache.get(pair) if cache is not None else None
    if reach is None:
        try:
            availability = _resolved_availability(config, root_entity, target_entity)
        except SemanticLayerError as exc:
            reach = PathReach(False, _refusal_reason(exc), ())
        else:
            reach = PathReach(
                availability["available"], availability["reason"], tuple(availability["path"])
            )
        if cache is not None:
            cache[pair] = reach
    return reach


def _path_has_temporal_validity(config: PackageConfig, path: Iterable[str]) -> bool:
    path = tuple(path or ())
    return bool(path) and not get_package_analysis(config).temporal_relationship_ids.isdisjoint(
        path
    )


def _history_coverage_note(*, dimension: bool = False) -> str:
    # When a row reads its version is the relationship's as_of: its time, or its day's end.
    if dimension:
        return "NULL means no history row was valid when the row read it, for this dimension."
    return "History-backed breakdowns preserve NULL when no history row was valid when read."


def _dimension_history_coverage(
    config: PackageConfig, dimension_id: str, root_entity: str = ""
) -> tuple[list[str], str]:
    dim = {row.id: row for row in config.dimensions}[dimension_id]
    if root_entity:
        availability = _path_reach(config, root_entity, dim.entity)
        if _path_has_temporal_validity(config, availability.path):
            note = _history_coverage_note(dimension=True)
            return [note], note
    if any(
        rel.temporal_validity and dim.entity in {rel.source_entity, rel.target_entity}
        for rel in config.relationships
    ):
        note = _history_coverage_note(dimension=True)
        return [note], note
    return [], ""


def _metric_history_coverage_notes(config: PackageConfig, root_entity: str) -> list[str]:
    notes = []
    for entity in config.entities:
        if entity.id == root_entity:
            continue
        availability = _path_reach(config, root_entity, entity.id)
        if _path_has_temporal_validity(config, availability.path):
            notes.append(_history_coverage_note())
            break
    return list(dict.fromkeys(notes))


def _value_domain_for_dimension(config: PackageConfig, dimension_id: str) -> Any | None:
    return next((row for row in config.value_domains if dimension_id in row.dimensions), None)


def _valid_values_lookup_hint(has_declared_domain: bool) -> str:
    if has_declared_domain:
        return ""
    return "No declared value domain is published; call valid-values with allow_live_query=true for an explicit governed valid-values lookup."


def _filter_value_rows(
    values: list[dict[str, Any]], *, search: str, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int, bool]:
    filtered = list(values)
    if search:
        search_norm = normalize_search_value(search)
        filtered = [
            row
            for row in filtered
            if search_norm in normalize_search_value(str(row.get("label", "")))
            or search_norm in normalize_search_value(str(row.get("value", "")))
            or any(
                search_norm in normalize_search_value(alias)
                for alias in row.get("aliases", []) or []
            )
        ]
    total = len(filtered)
    sliced = filtered[offset : offset + limit]
    has_more = offset + limit < total
    return sliced, total, has_more


def _declared_value_rows(
    domain: Any | None, *, search: str = "", offset: int = 0, limit: int = 100
) -> tuple[list[dict[str, Any]], int, bool]:
    rows = (
        [asdict(value) for value in list(getattr(domain, "values", []) or [])]
        if domain is not None
        else []
    )
    return _filter_value_rows(rows, search=search, offset=offset, limit=limit)
