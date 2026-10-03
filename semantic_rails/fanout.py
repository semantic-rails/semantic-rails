"""Fanout / path-safety analysis for measure rollups across relationships.

Exposes :func:`analyze_fanout` and :func:`resolve_path` — the compiler
calls these to decide whether a requested rollup across a chain of
relationships is safe (1:1, M:1, declared-as-rollup-safe) or unsafe
(many-side traversal with non-additive aggregations). Builds the
relationship graph from the package config and enumerates legal paths
under a hop limit.
"""

from __future__ import annotations

import os
import re
from collections import Counter, deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from .compiler_parts.indexes import RouteRefusal, RouteResolution, get_package_analysis
from .config_parts.route_rows import conflicting_rows, disagreeing_row, walk_entities
from .errors import SemanticLayerError
from .schema import DEFAULT_PATH_HOP_LIMIT, PackageConfig, PathPreferenceConfig, RelationshipConfig


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


def _plural(noun: str) -> str:
    head, space, last = noun.rpartition(" ")
    if last[-1:] == "y" and last[-2:-1].lower() not in set("aeiou"):
        last = f"{last[:-1]}ies"
    elif last[-1:] in {"s", "x", "z"} or last[-2:] in {"ch", "sh"}:
        last = f"{last}es"
    else:
        last = f"{last}s"
    return f"{head}{space}{last}"


def entity_label(config: PackageConfig, entity_id: str) -> str:
    entity = get_package_analysis(config).entities.get(entity_id)
    return (entity.label or entity.name) if entity is not None else entity_id


def _entity_keys(config: PackageConfig) -> dict[str, str]:
    """Each entity's authored key: its id's tail without the namespace prefix every entity id
    shares (``entity.bank_account`` -> ``account``)."""
    tails = {entity.id: entity.id.rpartition(".")[2] for entity in config.entities}
    prefix = os.path.commonprefix(list(tails.values()))
    prefix = prefix[: prefix.rfind("_") + 1]
    return {entity_id: _slug(tail[len(prefix) :]) for entity_id, tail in tails.items()}


def route_reading(config: PackageConfig, start: str, path: Sequence[str]) -> str:
    """``path`` in business words from package labels, e.g. "the District of the Account's
    Branch": every entity on the route, each hop to an entity named by its label. A hop whose
    entity pair has more than one relationship is named by its authored relationship label,
    else by its foreign-key columns, and a one-to-many hop reads "any of the …"."""
    analysis = get_package_analysis(config)
    pairs = Counter(
        frozenset((rel.source_entity, rel.target_entity)) for rel in analysis.relationships.values()
    )
    phrase = ""
    current = start
    for rel_id in path:
        rel = analysis.relationships[rel_id]
        forward = current == rel.source_entity
        reached = rel.target_entity if forward else rel.source_entity
        noun, qualifier = entity_label(config, reached), ""
        if pairs[frozenset((rel.source_entity, rel.target_entity))] > 1:
            default = (
                f"{entity_label(config, rel.source_entity)} to "
                f"{entity_label(config, rel.target_entity)}"
            )
            if rel.label in ("", default):
                qualifier = ", ".join(rel.source_columns or [rel.source_column])
            elif forward:
                noun = rel.label
            else:
                qualifier = rel.label
        many = not hop_is_functional(rel, current)
        word = (_plural(noun) if many else noun) + (f" ({qualifier})" if qualifier else "")
        phrase = (
            f"the {word} of {phrase}" if phrase else f"the {entity_label(config, start)}'s {word}"
        )
        if many:
            phrase = f"any of {phrase}"
        current = reached
    return phrase


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _option_ids(config: PackageConfig, start: str, routes: list[list[str]]) -> list[str]:
    """A slug per route, unique within the refusal and never an entity key: the waypoint
    entity keys plus the target key, or a direct hop's foreign-key column without its
    ``_id``/``_key``/``_code`` suffix (followed by the target key when that column names an
    entity, as a one-to-many hop's does); ``_2`` and up on a clash."""
    analysis = get_package_analysis(config)
    keys = _entity_keys(config)
    taken = set(keys.values())
    ids: list[str] = []
    for path in routes:
        entities = walk_entities(analysis.relationships, start, path)
        if len(path) == 1:
            rel = analysis.relationships[path[0]]
            columns = rel.source_columns or [rel.source_column]
            base = _slug("_".join(re.sub(r"_(id|key|code)$", "", col) for col in columns))
            if not base or base in taken:
                base = "_".join(part for part in (base, keys[entities[-1]]) if part)
        else:
            base = "_".join(keys[entity] for entity in entities[1:])
        base = base or "route"
        slug, suffix = base, 1
        while slug in taken:
            suffix += 1
            slug = f"{base}_{suffix}"
        taken.add(slug)
        ids.append(slug)
    return ids


def route_clarification(
    config: PackageConfig, start: str, target: str, routes: Sequence[Sequence[str]]
) -> dict[str, Any]:
    """The question an ambiguous route asks, in business words, and one option per route.

    Each option can be applied two ways: its ``decision`` is the ``graph.path_preferences``
    row that makes it the package default (``label`` = ``meaning``), and the same row in a
    query's ``route_decisions`` answers that one query with it. When that row would not load
    beside the package's rows, the option adds ``conflicts_with``: the rows to change before
    recording it (the decision still answers per query).
    """
    paths = [list(path) for path in routes]
    meanings = [route_reading(config, start, path) for path in paths]
    if len(set(meanings)) < len(meanings):  # two relationships with one label or one column
        meanings = [
            f"{meaning} ({', '.join(path)})" for meaning, path in zip(meanings, paths, strict=True)
        ]
    target_label, start_label = entity_label(config, target), entity_label(config, start)
    article = "an" if start_label[:1].lower() in set("aeiou") else "a"
    options: list[dict[str, Any]] = []
    for option_id, meaning, path in zip(
        _option_ids(config, start, paths), meanings, paths, strict=True
    ):
        option: dict[str, Any] = {
            "id": option_id,
            "meaning": meaning,
            "relationship_path": path,
            "decision": {**route_pin(start, target, path), "label": meaning},
        }
        conflicts = decision_conflicts(config, start, target, path)
        if conflicts:
            option["conflicts_with"] = conflicts
        options.append(option)
    return {
        "kind": "route",
        "apply": ["query", "package"],
        "question": f"Which {target_label} does the question mean for {article} {start_label}?",
        "options": options,
    }


def _route_decision_required(
    config: PackageConfig, start: str, target: str, routes: Sequence[Sequence[str]]
) -> SemanticLayerError:
    """The ``AMBIGUOUS_PATH`` refusal for the routes the ladder keeps, asking which one the
    question means (``details.clarification``)."""
    clarification = route_clarification(config, start, target, routes)
    return SemanticLayerError(
        "AMBIGUOUS_PATH",
        f"Ambiguous path from '{start}' to '{target}'. {clarification['question']} "
        + "; ".join(option["meaning"] for option in clarification["options"]),
        details={
            "reason": "route_decision_required",
            "start": start,
            "target": target,
            "clarification": clarification,
            "hint": (
                "Which route is meant is a business definition. Ask which option the question "
                "means, then resend the query with that option's decision in route_decisions "
                "(this query only), or record it as the package default with "
                "record_route_decision (a graph.path_preferences row)."
            ),
        },
    )


def route_label(config: PackageConfig, start: str, target: str, path: Sequence[str]) -> str:
    """The ``label`` of the package row deciding the pair, when ``path`` is its route."""
    return next(
        (
            row.label
            for row in config.path_preferences
            if (row.source_entity, row.target_entity) == (start, target)
            and list(row.relationship_path) == list(path)
        ),
        "",
    )


def pair_routes(config: PackageConfig, start: str, target: str) -> list[list[str]]:
    """Every route between the pair within the hop ceiling, whatever any row decides, sorted by
    length then ids: the routes the resolver chooses among, and the ones a query's row may
    take."""
    hop_limit = package_hop_limit(config)
    paths = enumerate_paths(get_package_analysis(config).graph, start, target, hop_limit)
    return sorted(paths, key=lambda path: (len(path), path))


def route_decision_basis(config: PackageConfig, start: str, target: str) -> str:
    """What a query's own row for the pair replaces: the rung of the package's ladder that
    resolves it (``decided``, ``colocated_key``, ``inherited`` or ``only_route``), or
    ``undecided`` when the package refuses it."""
    try:
        return package_route(config, start=start, target=target).basis
    except SemanticLayerError:
        return "undecided"


_query_routes: ContextVar[dict[tuple[str, str], tuple[str, ...]] | None] = ContextVar(
    "query_routes", default=None
)


@contextmanager
def query_route_decisions(rows: Mapping[tuple[str, str], Sequence[str]]) -> Iterator[None]:
    """Answer each (start, target) in ``rows`` by its route in this block: a query's own
    ``route_decisions``, checked by the caller. ``resolve_path`` consults them before the
    package's rows and its cache, and never caches them. No rows leaves an enclosing
    query's rows in force (a nested compile is part of that query)."""
    if not rows:
        yield
        return
    token = _query_routes.set({pair: tuple(path) for pair, path in rows.items()})
    try:
        yield
    finally:
        _query_routes.reset(token)


RouteChoice = tuple[str, str, tuple[str, ...]]
_route_choices: ContextVar[list[RouteChoice] | None] = ContextVar("route_choices", default=None)


@contextmanager
def recording_route_choices() -> Iterator[list[RouteChoice]]:
    """Collect, as (start, target, route), each route the SQL lowered in this block reads,
    nested compiles included, so the response can say how each was chosen (``route_note``)."""
    choices: list[RouteChoice] = []
    token = _route_choices.set(choices)
    try:
        yield choices
    finally:
        _route_choices.reset(token)


def record_route_choice(start: str, target: str, route: Sequence[str]) -> None:
    """Note the route from ``start`` to ``target`` the SQL reads."""
    choices = _route_choices.get()
    if choices is not None:
        choices.append((start, target, tuple(route)))


def route_basis(config: PackageConfig, start: str, target: str) -> str:
    """The rung of the route ladder that chose the pair's route (``resolve_path``):
    ``"query"`` (the query's own row), ``"decided"`` (the pair's own row),
    ``"colocated_key"`` (the start's own key), ``"inherited"`` (rows for pairs its routes walk
    through) or ``"only_route"``. ``""`` when the pair is refused."""
    try:
        return resolve_route(config, start=start, target=target).basis
    except SemanticLayerError:
        return ""


def route_note(
    config: PackageConfig, start: str, target: str, route: Sequence[str]
) -> RouteResolution | None:
    """The pair's resolution when the engine chose ``route``, the route the SQL read, among two
    or more, so the response says how. None when the pair has one route (nothing was chosen),
    is refused, or resolves to another route than the SQL read: a note names only the SQL's
    route, and says the same whatever the process resolved before.

    The pair's resolution is ``resolve_route``'s, which depends only on the package. Notes
    never enumerate routes for a pair the query resolved (its resolution is cached), and a
    decided pair only checks whether a second route fits the hop ceiling, with bounded
    reachability scans."""
    try:
        resolution = resolve_route(config, start=start, target=target)
    except SemanticLayerError:
        return None
    if resolution.routes[0] != tuple(route):
        return None
    if resolution.basis != "decided":
        return resolution if len(resolution.routes) > 1 else None
    analysis = get_package_analysis(config)
    pair = (start, target)
    if pair not in analysis.route_note_cache:
        analysis.route_note_cache[pair] = _has_multiple_routes(
            analysis.graph, start, target, package_hop_limit(config)
        )
    return resolution if analysis.route_note_cache[pair] else None


def decision_conflicts(
    config: PackageConfig, start: str, target: str, path: Sequence[str]
) -> list[dict[str, Any]]:
    """The package rows that the row recording ``path`` for the pair disagrees with
    (``route_rows.conflicting_rows``, the loader's check); empty when it would load beside
    them."""
    analysis = get_package_analysis(config)
    existing = [
        PathPreferenceConfig(source, end, list(route))
        for (source, end), route in analysis.path_preferences.items()
    ]
    row = PathPreferenceConfig(start, target, list(path))
    return [
        route_pin(other.source_entity, other.target_entity, other.relationship_path)
        for other in conflicting_rows(analysis.relationships, row, existing)
    ]


def offered_rows(
    config: PackageConfig, start: str, target: str, routes: Sequence[Sequence[str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The row that would record each route for the pair, offered only when it would load
    beside the package's rows (``decision_conflicts``), and, for each route whose row would
    not, the existing rows it disagrees with (``{"relationship_path", "rows"}``)."""
    offered: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for path in routes:
        rows = decision_conflicts(config, start, target, path)
        if rows:
            conflicts.append({"relationship_path": list(path), "rows": rows})
        else:
            offered.append(route_pin(start, target, list(path)))
    return offered, conflicts


def _shortest_path(
    graph: dict[str, list[tuple[str, str]]],
    start: str,
    target: str,
    hop_limit: int,
    excluded: str | None = None,
) -> tuple[str, ...] | None:
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


def _has_multiple_routes(
    graph: dict[str, list[tuple[str, str]]], start: str, target: str, hop_limit: int
) -> bool:
    """Check multiplicity without enumerating routes: find one bounded shortest path,
    then test reachability with each of its relationships removed. Any other simple path
    must omit at least one of those relationships. At most ``hop_limit + 1`` BFS scans,
    each visiting an entity once; cycles and parallel relationships need no special case.
    """
    path = _shortest_path(graph, start, target, hop_limit)
    return path is not None and any(
        _shortest_path(graph, start, target, hop_limit, rel_id) is not None for rel_id in path
    )


def resolve_path(
    config: PackageConfig, *, start: str, target: str
) -> tuple[list[str], list[list[str]]]:
    """The route from ``start`` to ``target``, and every route considered (the chosen first).

    The one route chooser: compilation, grain recovery, discovery and the direct key read
    all ask it (``resolve_route`` also says which rung chose). Which of two routes a question
    means is a business definition, so it never guesses one, by hop count, weight or
    otherwise. The ladder, over every route within the hop ceiling:

    0. **Query.** The query's own ``route_decisions`` row for exactly the pair
       (``query_route_decisions``) wins, for that query only; it is never cached.
    1. **Decided.** A ``graph.path_preferences`` row for exactly the pair wins.
    2. **The start's own key.** When exactly one route is a direct relationship from
       ``start`` that reaches at most one row (the start row holds the target's key), it is
       used, even where a row for another pair would point elsewhere. Two such keys (an
       origin and a destination) are not one: go on.
    3. **Inherited.** Every row holds wherever a route walks its pair: a route that passes
       through a row's pair by another part than the row's path (or, walked the other way,
       than that path reversed, when every hop allows it) is dropped.
    4. **Only route.** One route remains: it is used.
    5. Two or more remain: ``AMBIGUOUS_PATH`` (``reason: route_decision_required``), whatever
       their lengths, with ``details.clarification``: the question in business words and,
       per route, its meaning and the row that decides it (``route_clarification``).
    6. The rows dropped every route: ``PATH_NOT_FOUND`` (``reason: excluded_by_decision``),
       naming the rows.

    Resolutions and refusals are cached per pair, keyed by package inputs only.
    """
    resolution = resolve_route(config, start=start, target=target)
    return list(resolution.routes[0]), [list(path) for path in resolution.routes]


def resolve_route(config: PackageConfig, *, start: str, target: str) -> RouteResolution:
    """``resolve_path``'s resolution of the pair: every route considered, the chosen first,
    and the rung that chose it. Raises the pair's refusal."""
    chosen = (_query_routes.get() or {}).get((start, target))
    if chosen is not None:
        return RouteResolution((chosen,), "query")
    return package_route(config, start=start, target=target)


def package_route(config: PackageConfig, *, start: str, target: str) -> RouteResolution:
    """Rungs 1-6 of ``resolve_path``: the package's resolution of the pair, whatever a query
    decides."""
    analysis = get_package_analysis(config)
    pinned = analysis.path_preferences.get((start, target))
    if pinned is not None:
        return RouteResolution((tuple(pinned),), "decided")
    cached = analysis.path_cache.get((start, target))
    if cached is None:
        try:
            cached = _resolve_uncached(config, start, target)
        except SemanticLayerError as exc:
            cached = RouteRefusal(exc.code, str(exc), deepcopy(exc.details))
        analysis.path_cache[(start, target)] = cached
    if isinstance(cached, RouteRefusal):
        raise cached.error()
    return cached


def _resolve_uncached(config: PackageConfig, start: str, target: str) -> RouteResolution:
    """Rungs 2-6 of ``resolve_path``, for a pair with no row of its own."""
    analysis = get_package_analysis(config)
    hop_limit = package_hop_limit(config)
    routes = [tuple(path) for path in pair_routes(config, start, target)]
    if not routes:
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
    own_keys = [
        path
        for path in routes
        if len(path) == 1 and hop_is_functional(analysis.relationships[path[0]], start)
    ]
    if len(own_keys) == 1:
        others = (path for path in routes if path != own_keys[0])
        return RouteResolution((own_keys[0], *others), "colocated_key")
    kept: list[tuple[str, ...]] = []
    excluded_by: set[tuple[str, str]] = set()
    for path in routes:
        pair = disagreeing_row(
            walk_entities(analysis.relationships, start, path), path, analysis.route_rows
        )
        if pair is None:
            kept.append(path)
        else:
            excluded_by.add(pair)
    if len(kept) == 1:
        others = (path for path in routes if path != kept[0])
        if not excluded_by:
            return RouteResolution((kept[0],), "only_route")
        return RouteResolution((kept[0], *others), "inherited", tuple(sorted(excluded_by)))
    if kept:
        raise _route_decision_required(config, start, target, kept)
    rows = [route_pin(*pair, analysis.path_preferences[pair]) for pair in sorted(excluded_by)]
    raise SemanticLayerError(
        "PATH_NOT_FOUND",
        f"Every route from '{start}' to '{target}' within {hop_limit} hops walks an entity pair "
        "by another route than the package records for it: "
        + "; ".join(f"{row['source_entity']} -> {row['target_entity']}" for row in rows),
        details={
            "start": start,
            "target": target,
            "hop_limit": hop_limit,
            "reason": "excluded_by_decision",
            "rows": rows,
            "candidates": [list(path) for path in routes],
            "hint": (
                "A graph.path_preferences row holds wherever a route walks its pair, and no "
                "route here follows details.rows. Raise graph.path_policy.max_hops if a longer "
                "route follows them, or change the rows."
            ),
        },
    )


def _has_unique_inherited_route(
    config: PackageConfig, start: str, target: str, hop_limit: int
) -> bool:
    """Count at most two agreeing simple routes, pruning a disagreeing prefix."""
    analysis = get_package_analysis(config)

    def routes(entities: list[str], path: list[str]) -> Iterator[None]:
        if entities[-1] == target:
            yield None
            return
        if len(path) >= hop_limit:
            return
        for neighbor, rel_id in analysis.graph.get(entities[-1], []):
            if neighbor in entities:
                continue
            following, extended = [*entities, neighbor], [*path, rel_id]
            if disagreeing_row(following, extended, analysis.route_rows) is None:
                yield from routes(following, extended)

    found = routes([start], [])
    sentinel = object()
    return next(found, sentinel) is None and next(found, sentinel) is sentinel


def eligible_path_targets(config: PackageConfig, *, start: str) -> list[str]:
    """Proven resolvable targets without building or caching route refusal envelopes.

    One hop-bounded BFS excludes unreachable unpinned targets. Pins and unique functional
    direct routes then need no further search. Without inherited rows, other targets
    need at most ``hop_limit + 1`` BFS scans. With rows, a prefix-pruned search stops
    at the second agreeing route. Neither search writes caches or builds refusals.
    """
    analysis = get_package_analysis(config)
    hop_limit = package_hop_limit(config)
    pending = deque([(start, 0)])
    reachable = {start}
    while pending:
        node, hops = pending.popleft()
        if hops >= hop_limit:
            continue
        for neighbor, _rel_id in analysis.graph.get(node, []):
            if neighbor not in reachable:
                reachable.add(neighbor)
                pending.append((neighbor, hops + 1))
    direct: dict[str, list[list[str]]] = {}
    if hop_limit >= 1:
        for target, rel_id in analysis.graph.get(start, []):
            if target != start and hop_is_functional(analysis.relationships[rel_id], start):
                direct.setdefault(target, []).append([rel_id])
    eligible: list[str] = []
    for target in sorted(analysis.entities):
        if target == start:
            continue
        if (start, target) in analysis.path_preferences:
            eligible.append(target)
        elif target not in reachable:
            continue
        elif len(direct.get(target, [])) == 1:
            # The start's unique own key wins before inherited decisions.
            eligible.append(target)
        elif analysis.route_rows:
            if _has_unique_inherited_route(config, start, target, hop_limit):
                eligible.append(target)
        elif not _has_multiple_routes(analysis.graph, start, target, hop_limit):
            eligible.append(target)
    return eligible


def build_hop_profile(
    config: PackageConfig,
    *,
    root_entity: str,
    selected_paths: dict[str, list[str]],
    candidate_paths: dict[str, list[list[str]]] | None = None,
) -> dict[str, Any]:
    """First-class summary of the entity hops a compiled query performs.

    One entry per non-root target entity: the chosen relationship chain,
    per-hop direction / cardinality / safety, how many alternates the
    chooser considered, and which rung of the route ladder chose it
    (``route_basis``). The aggregate fields (``max_hop_count``,
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
            "route_basis": route_basis(config, root_entity, target),
        }
        label = route_label(config, root_entity, target, path)
        if label:
            targets[target]["route_label"] = label
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
