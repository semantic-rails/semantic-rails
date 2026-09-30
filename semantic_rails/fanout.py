"""Fanout / path-safety analysis for measure rollups across relationships.

Exposes :func:`analyze_fanout` and :func:`resolve_path` — the compiler
calls these to decide whether a requested rollup across a chain of
relationships is safe (1:1, M:1, declared-as-rollup-safe) or unsafe
(many-side traversal with non-additive aggregations). Builds the
relationship graph from the package config and enumerates legal paths
under a hop limit.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .compiler_parts.indexes import get_package_analysis
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


def is_functional_route(config: PackageConfig, start: str, path: list[str]) -> bool:
    """True when every hop of ``path``, walked from ``start``, reaches at most one row."""
    rel_index = get_package_analysis(config).relationships
    current = start
    for rel_id in path:
        rel = rel_index[rel_id]
        if not hop_is_functional(rel, current):
            return False
        current = rel.target_entity if current == rel.source_entity else rel.source_entity
    return True


def _ambiguous_path(
    start: str, target: str, routes: list[list[str]], hint: str
) -> SemanticLayerError:
    listed = "; ".join(" -> ".join(path) for path in routes)
    return SemanticLayerError(
        "AMBIGUOUS_PATH",
        f"Ambiguous path from '{start}' to '{target}': {listed}",
        details={
            "start": start,
            "target": target,
            "candidates": [list(path) for path in routes],
            "pins": [
                {"source_entity": start, "target_entity": target, "relationship_path": list(path)}
                for path in routes
            ],
            "hint": hint,
        },
    )


def resolve_path(
    config: PackageConfig, *, start: str, target: str
) -> tuple[list[str], list[list[str]]]:
    """The route from ``start`` to ``target``, and every route considered (the chosen first).

    The one route chooser: compilation, grain recovery, discovery and the direct key read
    all ask it. It never chooses between routes that can mean different things by hop count
    or by ``path_preference`` weights:

    1. A ``graph.path_preferences`` row for the pair wins.
    2. Eligible routes are every functional route (each hop reaches at most one row in the
       direction walked) and every route with a one-to-many hop that is no longer than the
       shortest functional route.
    3. Eligible routes of different lengths refuse ``AMBIGUOUS_PATH``, listing each route
       with the ``graph.path_preferences`` row that would pin it. Otherwise they share one
       length, and the lowest ``path_preference`` sum wins; equal sums refuse.
    4. With no functional route at all, the shortest route wins, then the lowest sum;
       equal length and sum refuse.
    """
    analysis = get_package_analysis(config)
    pinned = analysis.path_preferences.get((start, target))
    if pinned is not None:
        return list(pinned), [list(pinned)]
    cached = analysis.path_cache.get((start, target))
    if cached is not None:
        return list(cached[0]), [list(path) for path in cached]

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
    rel_index = analysis.relationships

    def _score(path: list[str]) -> tuple[int, int]:
        return len(path), sum(rel_index[rel_id].path_preference for rel_id in path)

    ranked = sorted(
        candidates,
        key=lambda path: (
            *_score(path),
            sum(1000 if rel_index[rel_id].safety == "unsafe" else 0 for rel_id in path),
        ),
    )
    functional = [is_functional_route(config, start, path) for path in ranked]
    if any(functional):
        # ``ranked`` is shortest first, so the first functional route is the shortest one.
        shortest = len(ranked[functional.index(True)])
        eligible = [
            path
            for path, is_functional in zip(ranked, functional, strict=True)
            if is_functional or len(path) <= shortest
        ]
        if len({len(path) for path in eligible}) > 1:
            raise _ambiguous_path(
                start,
                target,
                eligible,
                "These routes can give different answers, and neither hop count nor "
                "path_preference chooses between routes of different lengths. Pin the one the "
                "question means: add the graph.path_preferences row for it from details.pins "
                "(it applies only to queries from that source entity to that target entity).",
            )
    if len(ranked) > 1 and _score(ranked[0]) == _score(ranked[1]):
        raise _ambiguous_path(
            start,
            target,
            [path for path in ranked if _score(path) == _score(ranked[0])],
            "These routes can give different answers. "
            "Pin the one the question means: give the intended relationship a lower "
            "path_preference (it applies to every query), or add the "
            "graph.path_preferences row for it from details.pins (it applies only to "
            "queries from that source entity to that target entity).",
        )
    analysis.path_cache[(start, target)] = tuple(tuple(path) for path in ranked)
    return list(ranked[0]), [list(path) for path in ranked]


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
