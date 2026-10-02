"""Recorded routes (``graph.path_preferences`` rows) and the routes that agree with them.

A row decides which route a question between two entities means. The loader's package rows, a
query's ``route_decisions`` rows and Architect ``record_route_decision`` all check one row by
``check_route_row``, naming its entities through ``entity_references``. A ``PackageConfig``
keeps no authored entity key, so a query row names an entity by id or name, while the loader
and Architect, which read the package files, also accept the key.

A row records the route for its own pair, and every route that walks through that pair
inherits it: walked from the row's source to its target, the part between them must be the
row's path; walked the other way, it must be that path reversed, when every hop of the path
allows the reverse walk. A package's rows must agree with each other (``require_rows_agree``,
run by the loader and again by the package analysis, so a configuration built in code is held
to it too), a suggested row is offered only when it would agree with them, and the route
resolver drops each candidate route that disagrees with a row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

from ..errors import SemanticLayerError
from ..schema import EntityConfig, PathPreferenceConfig, RelationshipConfig

# A row's path, and the same path walked back (None when a hop does not allow that walk).
RowPaths = tuple[tuple[str, ...], tuple[str, ...] | None]


class RouteRowError(ValueError):
    """A route row that breaks the loader's rules; the caller words the error code and where
    the row came from."""


def entity_references(
    entities: Iterable[EntityConfig], keys: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Each accepted reference to an entity, mapped to its id: its name, the references in
    ``keys`` (the authored ``graph.entities`` keys, when the caller reads the package files)
    and its id, which wins."""
    entities = list(entities)
    return {
        **{entity.name: entity.id for entity in entities if entity.name},
        **dict(keys or {}),
        **{entity.id: entity.id for entity in entities},
    }


def check_route_row(
    row: Mapping[str, Any],
    *,
    entities: Mapping[str, str],
    relationships: Sequence[RelationshipConfig],
) -> PathPreferenceConfig:
    """Known entities (``entities`` is ``entity_references``) and relationships (by id, or its
    id without the ``relationship.`` prefix), each hop connecting from where the last one
    ended in an allowed direction, ending at the target."""
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


def walk_entities(
    relationships: Mapping[str, RelationshipConfig], start: str, path: Sequence[str]
) -> list[str]:
    """The entities ``path`` visits from ``start``, ``start`` first."""
    entities = [start]
    for rel_id in path:
        rel = relationships[rel_id]
        entities.append(
            rel.target_entity if entities[-1] == rel.source_entity else rel.source_entity
        )
    return entities


def row_paths(
    relationships: Mapping[str, RelationshipConfig], start: str, path: Sequence[str]
) -> RowPaths:
    """``path`` from ``start``, and ``path`` walked back from its end when every hop allows it."""
    for current, rel_id in zip(walk_entities(relationships, start, path), path, strict=False):
        rel = relationships[rel_id]
        back = "reverse" if current == rel.source_entity else "forward"
        allowed = {
            str(item).strip().lower()
            for item in list(rel.allowed_directions or ["forward", "reverse"])
        }
        if back not in allowed:
            return tuple(path), None
    return tuple(path), tuple(reversed(path))


def disagreeing_row(
    entities: Sequence[str], path: Sequence[str], rows: Mapping[tuple[str, str], RowPaths]
) -> tuple[str, str] | None:
    """The first row whose pair the route (``path``, visiting ``entities``) walks through by
    another part than the row records, or None when the route agrees with every row."""
    position = {entity: index for index, entity in enumerate(entities)}
    for pair, (forward, backward) in rows.items():
        source, target = position.get(pair[0]), position.get(pair[1])
        if source is None or target is None:
            continue
        expected, part = (
            (forward, path[source:target]) if source < target else (backward, path[target:source])
        )
        if expected is not None and tuple(part) != expected:
            return pair
    return None


def conflicting_rows(
    relationships: Mapping[str, RelationshipConfig],
    row: PathPreferenceConfig,
    rows: Iterable[PathPreferenceConfig],
) -> list[PathPreferenceConfig]:
    """The rows in ``rows`` that ``row`` disagrees with, the agreement check run both ways:
    ``row``'s path walks a row's pair by another part than that row records, or a row's path
    walks ``row``'s pair by another part than ``row`` records."""
    own = {
        (row.source_entity, row.target_entity): row_paths(
            relationships, row.source_entity, row.relationship_path
        )
    }
    entities = walk_entities(relationships, row.source_entity, row.relationship_path)
    found: list[PathPreferenceConfig] = []
    for other in rows:
        recorded = {
            (other.source_entity, other.target_entity): row_paths(
                relationships, other.source_entity, other.relationship_path
            )
        }
        other_entities = walk_entities(relationships, other.source_entity, other.relationship_path)
        if disagreeing_row(entities, row.relationship_path, recorded) or disagreeing_row(
            other_entities, other.relationship_path, own
        ):
            found.append(other)
    return found


def require_rows_agree(
    relationships: Mapping[str, RelationshipConfig],
    rows: Sequence[PathPreferenceConfig],
    *,
    path: str = "",
) -> None:
    """``INVALID_CONFIG`` unless the rows agree (``conflicting_rows``): a row holds wherever a
    route walks its pair, so two rows that record different parts for one pair are two
    definitions of it. Names, in package order, the first row that disagrees with rows before
    it and each of those rows."""

    def described(item: PathPreferenceConfig) -> str:
        return f"{item.source_entity} -> {item.target_entity} ({', '.join(item.relationship_path)})"

    def pinned(item: PathPreferenceConfig) -> dict[str, Any]:
        return {key: value for key, value in asdict(item).items() if key != "label"}

    for index, row in enumerate(rows):
        conflicts = conflicting_rows(relationships, row, rows[:index])
        if not conflicts:
            continue
        raise SemanticLayerError(
            "INVALID_CONFIG",
            (f"{path}: " if path else "")
            + f"graph.path_preferences rows disagree: the row for {described(row)} and the "
            f"rows for {'; '.join(map(described, conflicts))}: of each two, one walks the other's "
            "pair by another route than the other records. A row holds wherever a route walks "
            "its pair, so keep one definition of each pair: change or remove a row.",
            details={"rows": [pinned(item) for item in (*conflicts, row)]},
        )
