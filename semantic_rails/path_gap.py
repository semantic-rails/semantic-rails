"""What separates two entities a call can't join, for ``PATH_NOT_FOUND`` diagnostics.

``missing_link`` names the one change that would connect the call's start to its target: a
relationship that allows only the other walk, a key one side already holds, an entity that
holds both sides' keys, or an identifier both share that is a key of neither (a many-to-many
join, which needs a link entity). ``closest_dimensions`` and ``closest_measures`` rank what the
caller could ask for instead.

Nothing here chooses, caches or renders a route: it reads the package's entities, columns and
walkable relationship graph, and the targets route resolution already proved reachable. The
config is the caller's view, so it never names an object the caller can't see, and an answer
never depends on it.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .schema import PackageConfig

_WORD = re.compile(r"[^\W_]+")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Words that say a column identifies something without saying what.
_IDENTIFIER_WORDS = frozenset({"id", "key", "code", "number", "num", "no", "uuid", "sk", "pk"})
CLOSEST_DIMENSIONS = 5
CLOSEST_MEASURES = 3


def _words(*texts: Any) -> set[str]:
    return {word for text in texts for word in _WORD.findall(str(text or "").casefold())}


def _specific(column: str) -> bool:
    """Whether a column's name says what it identifies (``customer_id``, not ``id``)."""
    return bool(_words(column) - _IDENTIFIER_WORDS)


def _identifier(column: str) -> bool:
    words = _words(column)
    return bool(words & _IDENTIFIER_WORDS) and bool(words - _IDENTIFIER_WORDS)


def _hops(graph: dict[str, list[tuple[str, str]]], start: str, limit: int) -> dict[str, int]:
    """Entities a walk from ``start`` reaches within ``limit`` hops, by hop count."""
    hops = {start: 0}
    pending = deque([start])
    while pending:
        node = pending.popleft()
        if hops[node] >= limit:
            continue
        for neighbor, _rel in graph.get(node, []):
            if neighbor not in hops:
                hops[neighbor] = hops[node] + 1
                pending.append(neighbor)
    return hops


class _Columns:
    """Each entity's key and the columns it holds (dimension, key, foreign-key and join columns)."""

    def __init__(self, config: PackageConfig) -> None:
        self.keys = {row.id: list(row.key_columns) for row in config.entities}
        held: dict[str, set[str]] = {
            row.id: {column.casefold() for column in row.key_columns} for row in config.entities
        }
        for row in config.entities:
            for columns in row.foreign_keys.values():
                held[row.id].update(column.casefold() for column in columns)
        for dim in config.dimensions:
            if dim.entity in held and _COLUMN.match(dim.column or ""):
                held[dim.entity].add(dim.column.casefold())
        for rel in config.relationships:
            for entity, columns in (
                (rel.source_entity, rel.source_columns or [rel.source_column]),
                (rel.target_entity, rel.target_columns or [rel.target_column]),
            ):
                if entity in held:
                    held[entity].update(c.casefold() for c in columns if _COLUMN.match(c or ""))
        self.held = held

    def key_of(self, entity: str) -> set[str]:
        return {column.casefold() for column in self.keys.get(entity, [])}

    def holds_key_of(self, holder: str, keyed: str) -> bool:
        """Whether ``holder`` holds every column of ``keyed``'s declared key, and every one of
        those columns says what it identifies: an entity keyed by a bare ``id`` is never looked
        up by a column of the same name, which another table's own ``id`` would match."""
        key = self.key_of(keyed)
        if not key or holder == keyed or not key <= self.held.get(holder, set()):
            return False
        return all(_specific(column) for column in key)


def path_gap(
    config: PackageConfig, start: str, target: str, reachable: Sequence[str]
) -> dict[str, Any]:
    """``missing_link`` and ``nearest_entity`` for a pair no relationship chain joins, or
    nothing when a walkable route exists (the refusal is then a route choice) or no single
    change would connect them."""
    from .compiler_parts.indexes import get_package_analysis
    from .fanout import package_hop_limit

    analysis = get_package_analysis(config)
    if start not in analysis.entities or target not in analysis.entities:
        return {}
    if not analysis.entities[target].key_columns:
        return {}  # A row-grain target: nothing can look one of its rows up.
    limit = package_hop_limit(config)
    if target in _hops(analysis.graph, start, len(analysis.entities)):
        # A walk reaches it (past the hop limit, or by a route the package refuses): no link
        # is missing, and naming one would ask for a relationship that already exists.
        return {}
    hops = _hops(analysis.graph, start, limit)
    near = sorted(
        {start, *(entity for entity in reachable if entity in hops)},
        key=lambda entity: (hops[entity], entity),
    )
    near_set = set(near)
    into_target = sorted(
        entity
        for entity, edges in analysis.graph.items()
        if entity not in near_set
        and entity != target
        and any(neighbor == target for neighbor, _rel in edges)
    )
    far = [target, *into_target]
    columns = _Columns(config)

    def found(link: dict[str, Any], nearest: str) -> dict[str, Any]:
        return {"missing_link": link, "nearest_entity": nearest}

    pairs: dict[frozenset[str], list[Any]] = {}
    for rel in config.relationships:
        pairs.setdefault(frozenset((rel.source_entity, rel.target_entity)), []).append(rel)
    # The target itself first, then an entity one walk from it; nearest start side first. A
    # relationship already there (walkable only the other way) comes before a new one.
    for far_entity in far:
        for near_entity in near:
            for rel in pairs.get(frozenset((near_entity, far_entity)), []):
                return found(
                    {
                        "kind": "direction",
                        "relationship": rel.id,
                        "from": near_entity,
                        "to": far_entity,
                        "allowed_directions": list(rel.allowed_directions),
                        "fans_out": _fans_out(rel, near_entity),
                    },
                    near_entity,
                )
            for holder, keyed in ((near_entity, far_entity), (far_entity, near_entity)):
                if columns.holds_key_of(holder, keyed):
                    return found(
                        {
                            "kind": "lookup",
                            "source_entity": holder,
                            "target_entity": keyed,
                            "columns": list(columns.keys[keyed]),
                            "walk": "forward" if holder == near_entity else "reverse",
                        },
                        near_entity,
                    )
    far_set = set(far)
    bridges = []
    for bridge in sorted(analysis.entities):
        if bridge in near_set or bridge in far_set:
            continue
        near_index = next(
            (i for i, entity in enumerate(near) if columns.holds_key_of(bridge, entity)), None
        )
        far_index = next(
            (i for i, entity in enumerate(far) if columns.holds_key_of(bridge, entity)), None
        )
        if near_index is not None and far_index is not None:
            bridges.append((far_index, near_index, bridge))
    if bridges:
        far_index, near_index, bridge = min(bridges)
        near_entity, far_entity = near[near_index], far[far_index]
        return found(
            {
                "kind": "bridge",
                "via": bridge,
                "entities": [near_entity, far_entity],
                "columns": {
                    near_entity: list(columns.keys[near_entity]),
                    far_entity: list(columns.keys[far_entity]),
                },
            },
            near_entity,
        )
    for far_entity in far:
        for near_entity in near:
            shared = sorted(
                column
                for column in columns.held[near_entity] & columns.held[far_entity]
                if _identifier(column)
                and column not in columns.key_of(near_entity)
                and column not in columns.key_of(far_entity)
            )
            if shared:
                keyed_by = sorted(
                    entity
                    for entity in analysis.entities
                    if columns.key_of(entity) and columns.key_of(entity) <= set(shared)
                )
                link: dict[str, Any] = {
                    "kind": "many_to_many",
                    "entities": [near_entity, far_entity],
                    "columns": shared,
                }
                if keyed_by:
                    link["keyed_by"] = keyed_by[0]
                    link["keyed_by_columns"] = list(columns.keys[keyed_by[0]])
                return found(link, near_entity)
    return {}


def _fans_out(rel: Any, walked_from: str) -> bool | None:
    """Whether walking ``rel`` from ``walked_from`` meets many rows per row: a one-to-many walk
    by the relationship's declared cardinality (``N:1`` walked from its target, ``1:N`` from
    its source, ``M:N`` either way). None when the cardinality says neither."""
    cardinality = str(rel.cardinality or "").upper().replace(" ", "")
    if cardinality == "M:N":
        return True
    if cardinality not in {"1:1", "N:1", "1:N"}:
        return None
    forward = rel.source_entity == walked_from
    return cardinality == ("1:N" if forward else "N:1")


def _repeats(near: str, far: str, via: str = "") -> str:
    through = f" through '{via}'" if via else ""
    return (
        f" Each '{near}' row then meets many '{far}' rows{through}, so a measure read through "
        f"'{near}' repeats across them."
    )


def missing_link_fact(link: dict[str, Any]) -> str:
    """What is missing between the pair, in one clause (for the refusal's message)."""
    kind = link.get("kind")
    if kind == "direction":
        allowed = ", ".join(link["allowed_directions"]) or "none"
        return (
            f"relationship '{link['relationship']}' joins '{link['from']}' and '{link['to']}' "
            f"but allows no walk from '{link['from']}' (allowed_directions: {allowed})"
        )
    if kind == "lookup":
        return (
            f"'{link['source_entity']}' holds the key of '{link['target_entity']}' "
            f"({', '.join(link['columns'])}), but no relationship joins them"
        )
    if kind == "bridge":
        near, far = link["entities"]
        return (
            f"'{link['via']}' holds the keys of both '{near}' ({', '.join(link['columns'][near])}) "
            f"and '{far}' ({', '.join(link['columns'][far])}), but no relationship joins it to them"
        )
    if kind == "many_to_many":
        near, far = link["entities"]
        return (
            f"'{near}' and '{far}' share {', '.join(link['columns'])}, a key of neither, so a "
            "join on it is many-to-many"
        )
    return ""


def missing_link_fix(link: dict[str, Any]) -> str:
    """The change a package author could make to connect the pair, and what it does to the
    rows a call reads through it: whether a measure would repeat (fan out) across them."""
    kind = link.get("kind")
    if kind == "direction":
        near, far = link["from"], link["to"]
        fans_out = link.get("fans_out")
        consequence = (
            _repeats(near, far)
            if fans_out
            else f" Each '{near}' row then meets one '{far}' row, so nothing repeats."
            if fans_out is False
            else (
                f" Its cardinality doesn't say how many '{far}' rows each '{near}' row meets: "
                f"if many, a measure read through '{near}' repeats across them."
            )
        )
        return (
            f"Allow the walk from '{near}' in relationship '{link['relationship']}''s "
            f"allowed_directions, if its rows may be read that way.{consequence}"
        )
    if kind == "lookup":
        source, target = link["source_entity"], link["target_entity"]
        consequence = (
            _repeats(target, source)
            if link.get("walk") == "reverse"
            else f" Each '{source}' row then meets one '{target}' row, so nothing repeats."
        )
        return (
            f"Add a graph.relationships row from '{source}' to '{target}' on "
            f"{', '.join(link['columns'])}.{consequence}"
        )
    if kind == "bridge":
        near, far = link["entities"]
        return (
            f"Relate '{link['via']}' to '{near}' and to '{far}' on those keys, so a call walks "
            f"through it.{_repeats(near, link['via'])}"
        )
    if kind == "many_to_many":
        near, far = link["entities"]
        if link.get("keyed_by"):
            keyed_by = link["keyed_by"]
            on = ", ".join(link.get("keyed_by_columns") or [])
            return (
                f"Relate '{near}' and '{far}' each to '{keyed_by}' on its key ({on})."
                + _repeats(near, far, keyed_by)
            )
        return (
            f"Add a link entity with one row per pair of '{near}' and '{far}', related to both."
            + _repeats(near, far, "it")
        )
    return ""


def _ranked(
    rows: Iterable[tuple[str, set[str], tuple[Any, ...]]],
    wanted: set[str],
    population: Sequence[set[str]],
) -> list[str]:
    """Ids sharing a word with ``wanted``, rarer shared words first. A word on more than half of
    the population (a package prefix, "count") tells nothing apart, so it never counts."""
    freq = Counter(word for words in population for word in words)
    common = {word for word, n in freq.items() if len(population) >= 4 and n * 2 > len(population)}
    scored = []
    for object_id, words, tiebreak in rows:
        shared = (words & wanted) - common
        if shared:
            scored.append((-sum(1 / freq[word] for word in shared), *tiebreak, object_id))
    return [row[-1] for row in sorted(scored)]


def closest_dimensions(
    config: PackageConfig,
    start: str,
    reachable: Sequence[str],
    target: str,
    unreachable_items: Sequence[str],
) -> list[str]:
    """Dimensions on ``start`` or an entity it reaches whose names come closest to what the call
    asked for on ``target`` (or to the target itself), rarer shared words first."""
    entities = {row.id: row for row in config.entities}
    asked = {dim.id: dim for dim in config.dimensions if dim.id in set(unreachable_items)}
    wanted = set().union(*(_words(dim.column, dim.label) for dim in asked.values()))
    if not wanted and target in entities:
        wanted = _words(entities[target].label, entities[target].table)
    order = {entity: index for index, entity in enumerate([start, *reachable])}
    population = [_words(dim.column, dim.label) for dim in config.dimensions]
    rows = (
        (dim.id, _words(dim.column, dim.label), (order[dim.entity],))
        for dim in config.dimensions
        if dim.entity in order and dim.id not in asked
    )
    return _ranked(rows, wanted, population)[:CLOSEST_DIMENSIONS]


def closest_measures(
    config: PackageConfig,
    target: str,
    asked: Sequence[str],
    runnable: Callable[[str], bool] | None = None,
) -> list[str]:
    """Measures that do reach ``target``, that the caller could run (``runnable``: no access
    policy denies or withholds it), and whose names share words with the measures the call asked
    for. Each is a different measure: a stand-in to offer, never to switch to silently. Without
    ``runnable`` the caller's policies are unknown, so nothing is offered."""
    from .compiler_parts.indexes import get_package_analysis
    from .fanout import eligible_path_targets, package_hop_limit

    if runnable is None:
        return []
    measures = {row.id: row for row in config.measures}
    wanted = set().union(
        *(_words(measures[m].label, measures[m].name) for m in asked if m in measures)
    )
    if not wanted:
        return []
    analysis = get_package_analysis(config)
    reverse: dict[str, list[tuple[str, str]]] = {}
    for source, edges in analysis.graph.items():
        for end, rel in edges:
            reverse.setdefault(end, []).append((source, rel))
    reaches = _hops(reverse, target, package_hop_limit(config))
    population = [_words(row.label, row.name) for row in config.measures]
    rows = (
        (row.id, _words(row.label, row.name), (reaches[row.entity],))
        for row in config.measures
        if row.entity in reaches and row.id not in set(asked)
    )
    out: list[str] = []
    # A walk toward the target isn't a route the package answers by: check each one offered,
    # a bounded number of times.
    for measure_id in _ranked(rows, wanted, population)[: CLOSEST_MEASURES * 3]:
        entity = measures[measure_id].entity
        if not runnable(measure_id):
            continue
        if entity == target or target in eligible_path_targets(config, start=entity):
            out.append(measure_id)
        if len(out) == CLOSEST_MEASURES:
            break
    return out
