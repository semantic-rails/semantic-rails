"""Recorded routes (``graph.path_preferences`` rows) and the routes that agree with them.

A row records the route for its own pair, and every route that walks through that pair
inherits it: walked from the row's source to its target, the part between them must be the
row's path; walked the other way, it must be that path reversed, when every hop of the path
allows the reverse walk. The loader checks that the rows agree with each other, and the route
resolver drops each candidate route that disagrees with a row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..schema import RelationshipConfig

# A row's path, and the same path walked back (None when a hop does not allow that walk).
RowPaths = tuple[tuple[str, ...], tuple[str, ...] | None]


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
