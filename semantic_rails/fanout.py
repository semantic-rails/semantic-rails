"""Fanout / path-safety analysis for measure rollups across relationships.

Exposes :func:`analyze_fanout` and :func:`resolve_path` — the compiler
calls these to decide whether a requested rollup across a chain of
relationships is safe (1:1, M:1, declared-as-rollup-safe) or unsafe
(many-side traversal with non-additive aggregations). Builds the
relationship graph from the package config and enumerates legal paths
under a hop limit.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from .compiler_parts.indexes import RouteRefusal, get_package_analysis
from .errors import SemanticLayerError
from .schema import DEFAULT_PATH_HOP_LIMIT, PackageConfig, RelationshipConfig


def build_graph(config: PackageConfig) -> dict[str, list[tuple[str, str]]]:
    return {entity: list(edges) for entity, edges in get_package_analysis(config).graph.items()}


def package_hop_limit(config: PackageConfig) -> int:
    """Hop ceiling for path enumeration: ``graph.path_policy.max_hops``,
    falling back to the package default. Every compiler call site that
    enumerates paths must go through this instead of hardcoding a limit."""
    policy = getattr(config, "path_policy", None)
    if policy is None:
        return DEFAULT_PATH_HOP_LIMIT
    return int(policy.max_hops)


def _min_hops_unbounded(
    graph: dict[str, list[tuple[str, str]]], start: str, target: str
) -> int | None:
    """BFS hop count from start to target ignoring the hop limit. Used to
    tell 'no relationship chain exists at all' apart from 'a chain exists
    but is longer than the configured ceiling' in error envelopes."""
    if start == target:
        return 0
    frontier = [start]
    seen = {start}
    hops = 0
    while frontier:
        hops += 1
        next_frontier: list[str] = []
        for node in frontier:
            for neighbor, _rel_id in graph.get(node, []):
                if neighbor in seen:
                    continue
                if neighbor == target:
                    return hops
                seen.add(neighbor)
                next_frontier.append(neighbor)
        frontier = next_frontier
    return None


def enumerate_paths(
    graph: dict[str, list[tuple[str, str]]], start: str, target: str, hop_limit: int
) -> list[list[str]]:
    found: list[list[str]] = []

    def _dfs(node: str, path: list[str], visited: set[str]) -> None:
        if len(path) > hop_limit:
            return
        if node == target:
            found.append(list(path))
            return
        for neighbor, rel_id in graph.get(node, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            path.append(rel_id)
            _dfs(neighbor, path, visited)
            path.pop()
            visited.remove(neighbor)

    _dfs(start, [], {start})
    return found


def hop_is_functional(rel: RelationshipConfig, current_entity: str) -> bool:
    """True when walking ``rel`` from ``current_entity`` reaches at most one row: an N:1 or
    1:1 relationship walked forward, or a 1:N or 1:1 relationship walked in reverse."""
    cardinality = rel.cardinality.upper().replace(" ", "")
    if ":" not in cardinality:
        return False
    source_side, target_side = cardinality.split(":", 1)
    return (target_side if current_entity == rel.source_entity else source_side) == "1"


def route_pin(start: str, target: str, path: list[str]) -> dict[str, Any]:
    """The ``graph.path_preferences`` row that records ``path`` as the route for the pair."""
    return {"source_entity": start, "target_entity": target, "relationship_path": list(path)}


def route_meaning(config: PackageConfig, start: str, path: list[str]) -> str:
    """``path`` as a readable chain of labels, e.g. "Account → Owner → Home region": the start
    entity, then per hop the relationship's own label when the author gave one and the hop
    looks it up (walked forward), else the label of the entity it reaches."""
    analysis = get_package_analysis(config)

    def label(entity_id: str) -> str:
        entity = analysis.entities.get(entity_id)
        return (entity.label or entity.name) if entity is not None else entity_id

    chain = [label(start)]
    current = start
    for rel_id in path:
        rel = analysis.relationships[rel_id]
        forward = current == rel.source_entity
        current = rel.target_entity if forward else rel.source_entity
        default = f"{label(rel.source_entity)} to {label(rel.target_entity)}"
        chain.append(rel.label if forward and rel.label not in ("", default) else label(current))
    return " → ".join(chain)


def _route_decision_required(
    config: PackageConfig, start: str, target: str, routes: list[list[str]]
) -> SemanticLayerError:
    meanings = [route_meaning(config, start, path) for path in routes]
    if len(set(meanings)) < len(meanings):  # parallel roles without their own labels
        meanings = [
            f"{meaning} ({', '.join(path)})" for meaning, path in zip(meanings, routes, strict=True)
        ]
    return SemanticLayerError(
        "AMBIGUOUS_PATH",
        f"Ambiguous path from '{start}' to '{target}': " + "; ".join(meanings),
        details={
            "reason": "route_decision_required",
            "start": start,
            "target": target,
            "candidates": [list(path) for path in routes],
            "meanings": meanings,
            "pins": [route_pin(start, target, path) for path in routes],
            "hint": (
                "Which route is meant is a business definition. Record it once as a "
                "graph.path_preferences row in the package (details.pins has the row for each "
                "route); then every query uses it."
            ),
        },
    )


RouteChoice = tuple[str, str, tuple[tuple[str, ...], ...]]
_route_choices: ContextVar[list[RouteChoice] | None] = ContextVar("route_choices", default=None)


@contextmanager
def recording_route_choices() -> Iterator[list[RouteChoice]]:
    """Collect, as (start, target, routes), each route ``resolve_path`` returned that the SQL
    lowered in this block reads, nested compiles included, so the response can say how each
    was chosen (``route_basis``)."""
    choices: list[RouteChoice] = []
    token = _route_choices.set(choices)
    try:
        yield choices
    finally:
        _route_choices.reset(token)


def record_route_choice(start: str, target: str, routes: Sequence[Sequence[str]]) -> None:
    """Note a route the SQL reads, with every route ``resolve_path`` returned for the pair."""
    choices = _route_choices.get()
    if choices is not None:
        choices.append((start, target, tuple(tuple(path) for path in routes)))


def route_basis(
    config: PackageConfig, start: str, target: str, routes: Sequence[Sequence[str]]
) -> str:
    """How ``resolve_path`` chose the route it returned (``routes``) for a pair with two or
    more routes: ``"recorded"`` (a ``graph.path_preferences`` row) or ``"colocated_key"``
    (rule 3, the start's own key). ``""`` when the pair has one route, so nothing was chosen."""
    analysis = get_package_analysis(config)
    pair = (start, target)
    if pair in analysis.path_preferences:
        if pair not in analysis.route_note_cache:
            analysis.route_note_cache[pair] = _has_multiple_routes(
                analysis.graph, start, target, package_hop_limit(config)
            )
        return "recorded" if analysis.route_note_cache[pair] else ""
    return "colocated_key" if len(routes) > 1 else ""


def _has_multiple_routes(
    graph: dict[str, list[tuple[str, str]]], start: str, target: str, hop_limit: int
) -> bool:
    """Check multiplicity without enumerating routes: find one bounded shortest path,
    then test reachability with each of its relationships removed. Any other simple path
    must omit at least one of those relationships. At most ``hop_limit + 1`` BFS scans,
    each visiting an entity once; cycles and parallel relationships need no special case.
    """

    def shortest_path(excluded: str | None = None) -> tuple[str, ...] | None:
        pending: deque[tuple[str, tuple[str, ...]]] = deque([(start, ())])
        seen = {start}
        while pending:
            node, path = pending.popleft()
            if node == target:
                return path
            if len(path) >= hop_limit:
                continue
            for neighbor, rel_id in graph.get(node, []):
                if rel_id == excluded or neighbor in seen:
                    continue
                seen.add(neighbor)
                pending.append((neighbor, (*path, rel_id)))
        return None

    path = shortest_path()
    return path is not None and any(shortest_path(rel_id) is not None for rel_id in path)


def resolve_path(
    config: PackageConfig, *, start: str, target: str
) -> tuple[list[str], list[list[str]]]:
    """The route from ``start`` to ``target``, and every route considered (the chosen first).

    The one route chooser: compilation, grain recovery, discovery and the direct key read
    all ask it. Which of two routes a question means is a business definition, so it never
    guesses one, by hop count or otherwise:

    1. A ``graph.path_preferences`` row for the pair wins.
    2. Exactly one route: it is used.
    3. Otherwise, when exactly one route is a direct relationship from ``start`` that reaches
       at most one row (the start row holds the target's key), it is used, and every route is
       returned with it.
    4. Otherwise ``AMBIGUOUS_PATH`` (``reason: route_decision_required``), whatever the
       routes' lengths, naming each route, its meaning and the row that would record it.

    So more than one route comes back exactly when rule 3 chose. Routes and refusals are
    cached per pair; ``route_basis`` says how a returned route was chosen.
    """
    pinned = get_package_analysis(config).path_preferences.get((start, target))
    if pinned is not None:
        return list(pinned), [list(pinned)]
    resolved = _unpinned_resolution(config, start, target)
    if isinstance(resolved, RouteRefusal):
        raise resolved.error()
    return list(resolved[0]), [list(path) for path in resolved]


def _unpinned_resolution(
    config: PackageConfig, start: str, target: str
) -> tuple[tuple[str, ...], ...] | RouteRefusal:
    """Rules 2-4 of ``resolve_path`` for the pair, cached: its routes or its refusal."""
    cache = get_package_analysis(config).path_cache
    cached = cache.get((start, target))
    if cached is None:
        try:
            cached = tuple(tuple(path) for path in _resolve_uncached(config, start, target))
        except SemanticLayerError as exc:
            cached = RouteRefusal(exc.code, str(exc), deepcopy(exc.details))
        cache[(start, target)] = cached
    return cached


def _resolve_uncached(config: PackageConfig, start: str, target: str) -> list[list[str]]:
    """Every route for an unpinned pair, the chosen first (rules 2-4 of ``resolve_path``)."""
    analysis = get_package_analysis(config)
    hop_limit = package_hop_limit(config)
    candidates = enumerate_paths(analysis.graph, start, target, hop_limit)
    if not candidates:
        reachable_at = _min_hops_unbounded(analysis.graph, start, target)
        if reachable_at is not None:
            raise SemanticLayerError(
                "PATH_NOT_FOUND",
                f"'{target}' is reachable from '{start}' in {reachable_at} hops, "
                f"but the path policy allows at most {hop_limit}",
                details={
                    "start": start,
                    "target": target,
                    "hop_limit": hop_limit,
                    "reason": "hop_limit_exceeded",
                    "reachable_at_hops": reachable_at,
                    "hint": (
                        "Raise graph.path_policy.max_hops to allow this traversal, or "
                        "author a shortcut relationship / aggregate relation if this "
                        "hop pattern is common."
                    ),
                },
            )
        raise SemanticLayerError(
            "PATH_NOT_FOUND",
            f"No path from '{start}' to '{target}'",
            details={
                "start": start,
                "target": target,
                "hop_limit": hop_limit,
                "reason": "no_relationship_chain",
            },
        )
    routes = sorted(candidates, key=lambda path: (len(path), path))
    if len(routes) == 1:
        return routes
    direct = [
        path
        for path in routes
        if len(path) == 1 and hop_is_functional(analysis.relationships[path[0]], start)
    ]
    if len(direct) == 1:
        return [direct[0], *(path for path in routes if path != direct[0])]
    raise _route_decision_required(config, start, target, routes)


def build_hop_profile(
    config: PackageConfig,
    *,
    root_entity: str,
    selected_paths: dict[str, list[str]],
    candidate_paths: dict[str, list[list[str]]] | None = None,
) -> dict[str, Any]:
    """First-class summary of the entity hops a compiled query performs.

    One entry per non-root target entity: the chosen relationship chain,
    per-hop direction / cardinality / safety, and how many alternates the
    chooser considered. The aggregate fields (``max_hop_count``,
    ``long_hop_targets``) are the acceleration-layer signal: queries that
    repeatedly cross 3+ relationships to reach the same target are the
    candidates for shortcut relationships, entity colocation, or authored
    aggregate relations.
    """
    rel_index = get_package_analysis(config).relationships
    candidates = candidate_paths or {}
    targets: dict[str, Any] = {}
    max_hop_count = 0
    for target, path in sorted(selected_paths.items()):
        hops: list[dict[str, Any]] = []
        current = root_entity
        for rel_id in path:
            rel = rel_index.get(rel_id)
            if rel is None:
                continue
            forward = current == rel.source_entity
            next_entity = rel.target_entity if forward else rel.source_entity
            hops.append(
                {
                    "relationship": rel.id,
                    "from_entity": current,
                    "to_entity": next_entity,
                    "direction": "forward" if forward else "reverse",
                    "cardinality": rel.cardinality,
                    "safety": rel.safety,
                    "temporal": bool(rel.temporal_validity),
                }
            )
            current = next_entity
        targets[target] = {
            "hop_count": len(path),
            "path": list(path),
            "hops": hops,
            "alternates_considered": max(0, len(candidates.get(target, [])) - 1),
        }
        max_hop_count = max(max_hop_count, len(path))
    return {
        "root_entity": root_entity,
        "hop_limit": package_hop_limit(config),
        "max_hop_count": max_hop_count,
        "long_hop_targets": sorted(
            target for target, row in targets.items() if row["hop_count"] >= 3
        ),
        "targets": targets,
    }


def _directional_status(
    rel: RelationshipConfig, *, current_entity: str, time_bound: bool = False
) -> str:
    card = rel.cardinality.upper()
    if ":" not in card:
        return rel.safety
    if time_bound and rel.temporal_validity and rel.safety != "unsafe":
        return "safe"
    left, right = [part.strip() for part in card.split(":", 1)]
    forward = current_entity == rel.source_entity
    if left == "1" and right == "1":
        return "safe"
    if left == "1" and right == "N":
        return ("unsafe" if rel.safety == "unsafe" else "requires_rewrite") if forward else "safe"
    if left == "N" and right == "1":
        return "safe" if forward else ("unsafe" if rel.safety == "unsafe" else "requires_rewrite")
    return rel.safety


def analyze_fanout(
    config: PackageConfig,
    start_entity: str,
    path: list[str],
    *,
    time_bound_relationships: set[str] | None = None,
) -> dict[str, Any]:
    rel_index = get_package_analysis(config).relationships
    temporal_overrides = time_bound_relationships or set()
    joins: list[dict[str, Any]] = []
    current_entity = start_entity
    unsafe: list[str] = []
    rewrite_required: list[str] = []
    for rel_id in path:
        rel = rel_index[rel_id]
        traversal = "forward" if current_entity == rel.source_entity else "reverse"
        status = _directional_status(
            rel, current_entity=current_entity, time_bound=rel_id in temporal_overrides
        )
        row = asdict(rel)
        row["traversal"] = traversal
        row["directional_safety"] = status
        joins.append(row)
        if status == "unsafe":
            unsafe.append(rel.id)
        elif status == "requires_rewrite":
            rewrite_required.append(rel.id)
        current_entity = rel.target_entity if traversal == "forward" else rel.source_entity
    if unsafe:
        raise SemanticLayerError(
            "FANOUT_UNSAFE",
            "Unsafe path expansion",
            details={"relationships": unsafe, "path": path},
        )
    return {
        "path": list(path),
        "relationships": joins,
        "status": "rewrite_required" if rewrite_required else "ok",
        "requires_rewrite_relationships": rewrite_required,
    }


def one_to_many_descent(analysis: dict[str, Any], entity_keys: dict[str, list[str]]) -> bool:
    """True when a rewrite-required path only goes down one-to-many hops, then looks up.

    Every hop that needs a rewrite must be a plain one-to-many (the reverse of N:1, or a
    forward 1:N) with no temporal validity, whose one side joins on exactly its declared key
    (``entity_keys``), and it must come before any many-to-one lookup. Then each row of the
    start entity has its own set of target rows, so a query can keep one row per (start key,
    output grain) and count every start row once per group. A lookup followed by a fan-out
    (orders -> customer -> sessions), an M:N hop or a join off the key relates the two
    entities many-to-many, and stays refused.
    """
    descended = looked_up = False
    for row in analysis.get("relationships", []) or []:
        cardinality = str(row.get("cardinality", "")).upper().replace(" ", "")
        forward = row.get("traversal") == "forward"
        status = row.get("directional_safety")
        if status == "requires_rewrite":
            side = "source" if forward else "target"
            one = str(row.get(f"{side}_entity", ""))
            columns = list(row.get(f"{side}_columns") or [row.get(f"{side}_column")])
            if (
                cardinality != ("1:N" if forward else "N:1")
                or looked_up
                or row.get("temporal_validity")
                or sorted(columns) != sorted(entity_keys.get(one, []))
            ):
                return False
            descended = True
        elif status != "safe":
            return False
        elif cardinality != "1:1":
            looked_up = True
    return descended


def filter_only_semijoin(analysis: dict[str, Any]) -> bool:
    """A declared non-temporal path can filter rows with EXISTS without expanding them.

    Unlike grouped de-duplication, this does not need a descent before every lookup or a
    join on the parent's primary key: the correlation uses the authored join columns.
    Undeclared cardinalities, unsafe hops and temporal paths still need other semantics.
    """
    rows = analysis.get("relationships", []) or []
    return bool(rows) and all(
        str(row.get("cardinality", "")).upper().replace(" ", "") in {"1:1", "N:1", "1:N"}
        and row.get("directional_safety") in {"safe", "requires_rewrite"}
        and not row.get("temporal_validity")
        for row in rows
    )
