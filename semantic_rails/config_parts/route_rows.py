"""The loader's rules for one route row.

A route row decides which route a question between two entities means. Package
``graph.path_preferences`` rows, a query's ``route_decisions`` rows and Architect
``record_route_decision`` all check rows here, so the three accept exactly the same rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..schema import PathPreferenceConfig, RelationshipConfig


class RouteRowError(ValueError):
    """A route row that breaks the loader's rules; the caller words the error code and where
    the row came from."""


def check_route_row(
    row: Mapping[str, Any],
    *,
    entities: Mapping[str, str],
    relationships: Sequence[RelationshipConfig],
) -> PathPreferenceConfig:
    """Known entities (``entities`` maps each accepted reference to an entity id) and
    relationships (by id, or its id without the ``relationship.`` prefix), each hop connecting
    from where the last one ended in an allowed direction, ending at the target."""
    rel_lookup: dict[str, RelationshipConfig] = {}
    for known_rel in relationships:
        rel_lookup[known_rel.id] = known_rel
        _, _, suffix = known_rel.id.partition(".")
        if suffix:
            rel_lookup.setdefault(suffix, known_rel)
    source_ref = str(row.get("source_entity", "")).strip()
    target_ref = str(row.get("target_entity", "")).strip()
    for label, ref in (("source_entity", source_ref), ("target_entity", target_ref)):
        if ref not in entities:
            raise RouteRowError(f"row references unknown {label} '{ref}'")
    source_entity = entities[source_ref]
    target_entity = entities[target_ref]
    preferred = row.get("preferred_paths")
    if preferred is not None:
        paths_raw = list(preferred or [])
        if len(paths_raw) != 1:
            raise RouteRowError(
                f"for {source_ref} -> {target_ref} must declare exactly one preferred path "
                f"(got {len(paths_raw)})"
            )
        rel_refs = [str(item) for item in list(paths_raw[0] or [])]
    else:
        rel_refs = [str(item) for item in list(row.get("relationship_path", []) or [])]
    if not rel_refs:
        raise RouteRowError(f"for {source_ref} -> {target_ref} declares an empty path")
    resolved: list[str] = []
    current = source_entity
    for rel_ref in rel_refs:
        rel = rel_lookup.get(rel_ref)
        if rel is None:
            raise RouteRowError(
                f"for {source_ref} -> {target_ref} references unknown relationship '{rel_ref}'"
            )
        directions = {
            str(item).strip().lower()
            for item in list(rel.allowed_directions or ["forward", "reverse"])
        }
        if current == rel.source_entity and "forward" in directions:
            current = rel.target_entity
        elif current == rel.target_entity and "reverse" in directions:
            current = rel.source_entity
        else:
            raise RouteRowError(
                f"for {source_ref} -> {target_ref}: relationship '{rel.id}' does not connect "
                f"from '{current}' (or traversal in that direction is not allowed)"
            )
        resolved.append(rel.id)
    if current != target_entity:
        raise RouteRowError(
            f"path for {source_ref} -> {target_ref} ends at '{current}', not the declared target"
        )
    return PathPreferenceConfig(
        source_entity=source_entity,
        target_entity=target_entity,
        relationship_path=resolved,
        label=str(row.get("label", "") or "").strip(),
    )
