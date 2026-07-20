"""Reachability and history-coverage answers for catalog objects.

Discover, inspect, and valid-values all need the same set of questions
answered about a given object:

* "From the current root entity, can I get to this dimension / measure /
  entity at all, and on what relationship path?" — ``_path_availability``.
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
from typing import Any

from ..errors import SemanticLayerError
from ..fanout import analyze_fanout, choose_path, package_hop_limit
from ..schema import PackageConfig
from .relevance import _norm


def _relationships_by_id(config: PackageConfig) -> dict[str, Any]:
    return {row.id: row for row in config.relationships}


def _dimensions_by_id(config: PackageConfig) -> dict[str, Any]:
    return {row.id: row for row in config.dimensions}


def _path_availability(
    config: PackageConfig, root_entity: str, target_entity: str
) -> dict[str, Any]:
    if not root_entity or target_entity == root_entity:
        return {"available": True, "reason": "", "path": [], "candidates": []}
    try:
        path, candidates = choose_path(
            config, start=root_entity, target=target_entity, hop_limit=package_hop_limit(config)
        )
        analysis = analyze_fanout(config, root_entity, path)
        status = analysis["status"]
        if status == "ok":
            return {
                "available": True,
                "reason": "",
                "path": path,
                "candidates": candidates,
                "analysis": analysis,
            }
        return {
            "available": False,
            "reason": "requires leaf rewrite or grain-aware plan",
            "path": path,
            "candidates": candidates,
            "analysis": analysis,
        }
    except SemanticLayerError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "path": [],
            "candidates": [],
            "error_code": exc.code,
            "details": exc.details,
        }


def _path_has_temporal_validity(config: PackageConfig, path: Iterable[str]) -> bool:
    relationships = _relationships_by_id(config)
    return any(
        bool(relationships.get(rel_id) and relationships[rel_id].temporal_validity)
        for rel_id in list(path or [])
    )


def _history_coverage_note(*, dimension: bool = False) -> str:
    if dimension:
        return (
            "NULL means no valid history row existed at the query time anchor for this dimension."
        )
    return "History-backed breakdowns preserve NULL when no valid history row exists at the query time anchor."


def _dimension_history_coverage(
    config: PackageConfig, dimension_id: str, root_entity: str = ""
) -> tuple[list[str], str]:
    dim = _dimensions_by_id(config)[dimension_id]
    if root_entity:
        availability = _path_availability(config, root_entity, dim.entity)
        if _path_has_temporal_validity(config, availability.get("path", [])):
            note = _history_coverage_note(dimension=True)
            return [note], note
    relationships = _relationships_by_id(config)
    if any(
        rel.temporal_validity and dim.entity in {rel.source_entity, rel.target_entity}
        for rel in relationships.values()
    ):
        note = _history_coverage_note(dimension=True)
        return [note], note
    return [], ""


def _metric_history_coverage_notes(config: PackageConfig, root_entity: str) -> list[str]:
    notes = []
    for entity in config.entities:
        if entity.id == root_entity:
            continue
        availability = _path_availability(config, root_entity, entity.id)
        if _path_has_temporal_validity(config, availability.get("path", [])):
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
        search_norm = _norm(search)
        filtered = [
            row
            for row in filtered
            if search_norm in _norm(str(row.get("label", "")))
            or search_norm in _norm(str(row.get("value", "")))
            or any(search_norm in _norm(alias) for alias in row.get("aliases", []) or [])
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
