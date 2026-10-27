"""An entity's place in the join graph, for its card.

``relationships`` lists each relationship the entity takes part in, seen from its side, and
``reachable_entities`` the entities a query rooted on it reaches within the hop ceiling. Both
read the configuration they are given, which on every transport is the caller's visible view:
a hidden relationship or entity is never listed, and reachability is the view's own route
resolution (``fanout.package_route``), so an entity whose package route reads a hidden object
is not reachable for that caller; another route is never chosen in its place.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..compiler_parts.indexes import get_package_analysis
from ..errors import SemanticLayerError
from ..fanout import package_hop_limit, package_route, route_note
from ..schema import PackageConfig, RelationshipConfig

# A minimal card keeps this many entries of each list and counts the rest.
MINIMAL_LIST_CAP = 10


def relationship_from(rel: RelationshipConfig, entity_id: str) -> dict[str, Any]:
    """``rel`` as ``entity_id`` takes part in it: the other entity, the cardinality from this
    side (this entity's rows to the other's), the ways a route may walk it (``to`` the other
    entity, ``from`` it) and the join columns as (this, other) pairs. An entity related to
    itself reads the relationship from its source side."""
    outward = rel.source_entity == entity_id
    directions = rel.allowed_directions or ["forward", "reverse"]
    allowed = {str(item).strip().lower() for item in directions}
    source = list(rel.source_columns or [rel.source_column])
    target = list(rel.target_columns or [rel.target_column])
    cardinality = rel.cardinality
    if not outward and cardinality != "M:N":
        cardinality = ":".join(reversed(cardinality.split(":")))
    here, there = (source, target) if outward else (target, source)
    # A forward walk goes from the source to the target.
    to, back = ("forward", "reverse") if outward else ("reverse", "forward")
    row: dict[str, Any] = {
        "id": rel.id,
        "entity": rel.target_entity if outward else rel.source_entity,
        "cardinality": cardinality,
        "directions": [walk for walk, way in (("to", to), ("from", back)) if way in allowed],
        # Paired as the compiler joins them.
        "on": [[mine, theirs] for mine, theirs in zip(here, there, strict=True)],
    }
    if rel.temporal_validity:
        row["temporal_validity"] = dict(rel.temporal_validity)
    return row


def entity_relationships(config: PackageConfig, entity_id: str) -> list[dict[str, Any]]:
    """Each relationship ``entity_id`` takes part in, in package order (``relationship_from``)."""
    return [
        relationship_from(rel, entity_id)
        for rel in config.relationships
        if entity_id in (rel.source_entity, rel.target_entity)
    ]


def reachable_entities(config: PackageConfig, entity_id: str) -> list[dict[str, Any]]:
    """Every entity a route from ``entity_id`` reaches within the hop ceiling, nearest first:
    ``via`` the package's route, ``recorded`` when recorded route rows chose it among the
    caller's routes (an answer's ``basis: recorded``, by the same ``route_note``), or
    ``route_decision_required`` when the question has to choose (``routes``). A pair the
    package refuses otherwise is left out."""
    graph = get_package_analysis(config).graph
    ceiling = package_hop_limit(config)
    hops = {entity_id: 0}
    pending = deque([entity_id])
    while pending:
        node = pending.popleft()
        if hops[node] < ceiling:
            for neighbor, _relationship in graph.get(node, []):
                if neighbor not in hops:
                    hops[neighbor] = hops[node] + 1
                    pending.append(neighbor)
    rows: list[dict[str, Any]] = []
    for target in sorted(hops, key=lambda entity: (hops[entity], entity)):
        if target == entity_id:
            continue
        try:
            resolution = package_route(config, start=entity_id, target=target)
        except SemanticLayerError as exc:
            if exc.code == "AMBIGUOUS_PATH":
                rows.append({"id": target, "route_decision_required": True})
            continue
        route = resolution.routes[0]
        row: dict[str, Any] = {"id": target, "via": list(route)}
        note = route_note(config, entity_id, target, route)
        if note is not None and note.basis in {"decided", "inherited"}:
            row["recorded"] = True
        elif note is not None and note.basis == "default" and len(note.routes) > 1:
            row["default"] = note.by
        rows.append(row)
    return rows


def entity_graph(config: PackageConfig, entity_id: str, *, minimal: bool) -> dict[str, Any]:
    """An entity card's ``relationships`` and ``reachable_entities``. A minimal card keeps the
    first ``MINIMAL_LIST_CAP`` of each, without the routes, and counts the rest in
    ``more_relationships`` and ``more_reachable_entities``."""
    graph: dict[str, Any] = {
        "relationships": entity_relationships(config, entity_id),
        "reachable_entities": reachable_entities(config, entity_id),
    }
    if not minimal:
        return graph
    for key in ("relationships", "reachable_entities"):
        rows = graph[key]
        graph[key] = [
            {field: value for field, value in row.items() if field != "via"}
            for row in rows[:MINIMAL_LIST_CAP]
        ]
        if len(rows) > MINIMAL_LIST_CAP:
            graph[f"more_{key}"] = len(rows) - MINIMAL_LIST_CAP
    return graph
