"""What separates two entities a call can't join, for ``PATH_NOT_FOUND`` diagnostics.

``missing_link`` names the one change that would connect the call's start to its target: a
relationship that allows only the other walk, a key one side already holds, an entity that
holds both sides' keys, or an identifier both share that is a key of neither (a many-to-many
join, which needs a link entity). ``closest_dimensions`` ranks the reachable dimensions the
caller could group by instead, each a different dimension to confirm with them first.

``lookalike_swaps`` pairs the call's measures on the start with the measures of the same name
on another model (``lookalike_fact`` and ``lookalike_rows_in_view`` state them). The runtime
keeps a pair only when the call compiles with it, in the caller's own request scope.
``measure_lookalikes`` lists one measure's look-alikes, for the notice on its card.

Nothing here chooses, caches or renders a route: it reads the package's entities, columns and
walkable relationship graph, and the targets route resolution already proved reachable. The
config is the caller's view, so it never names an object the caller can't see, and an answer
never depends on it.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .expressions import expr_to_dict
from .schema import PackageConfig

_WORD = re.compile(r"[^\W_]+")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Words that say a column identifies something without saying what.
_IDENTIFIER_WORDS = frozenset({"id", "key", "code", "number", "num", "no", "uuid", "sk", "pk"})
CLOSEST_DIMENSIONS = 5


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
                # Keyed by the near side's own key: one bridge row per near row.
                "via_keyed_by_near": columns.key_of(bridge) == columns.key_of(near_entity),
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
        via = str(link["via"])
        consequence = (
            f" Each '{near}' row then meets one '{via}' row, so nothing repeats."
            if link.get("via_keyed_by_near")
            else _repeats(near, via)
        )
        return (
            f"Relate '{via}' to '{near}' and to '{far}' on those keys, so a call walks "
            f"through it.{consequence}"
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
            f" Each '{near}' row then meets many '{far}' rows through it, so a measure read "
            f"through '{near}' repeats across them."
        )
    return ""


# --- The key columns the pair shares ------------------------------------------------------------


_DATED = frozenset({"date", "timestamp", "datetime", "time"})


def shared_key(config: PackageConfig, start: str, target: str) -> dict[str, Any]:
    """The columns of either entity's declared key that both ``start`` and ``target`` hold, each
    saying what it identifies (``shared_key_columns``, empty when they share none, spelled as
    the key declares them) and, when there are any, whether they hold ``target``'s whole key
    (``shared_key_unique_on_target``): only then does each ``start`` row match at most one
    ``target`` row on them.

    Columns that only date the rows (a load, snapshot or validity date: a dimension of either
    side typed as a date or time) identify nothing the two share, unless they are one side's
    whole key, as a calendar's date is."""
    columns = _Columns(config)
    keys = columns.key_of(start) | columns.key_of(target)
    shared = {
        column
        for column in columns.held.get(start, set()) & columns.held.get(target, set())
        if column in keys and _specific(column)
    }
    dated = {
        dim.column.casefold()
        for dim in config.dimensions
        if dim.entity in (start, target) and dim.column and dim.data_type.casefold() in _DATED
    }
    if shared <= dated and shared not in (columns.key_of(start), columns.key_of(target)):
        shared = set()
    if not shared:
        return {"shared_key_columns": []}
    # A relationship's columns compare as written, so name them as the keys do.
    spelled = {
        column.casefold(): column
        for entity in (start, target)
        for column in columns.keys.get(entity, [])
    }
    key = columns.key_of(target)
    return {
        "shared_key_columns": sorted(spelled.get(column, column) for column in shared),
        "shared_key_unique_on_target": bool(key) and key <= shared,
    }


def _shared(details: dict[str, Any]) -> tuple[str, str, list[str], str]:
    shared = list(details.get("shared_key_columns") or [])
    return str(details.get("start")), str(details.get("target")), shared, ", ".join(shared)


def _them(shared: Sequence[str]) -> str:
    return "it" if len(shared) == 1 else "them"


def shared_key_fact(details: dict[str, Any]) -> str:
    """What the pair shares, in one clause (for the refusal's message): no key column, the
    target's whole key, or key columns on which one start row can match many target rows."""
    if "shared_key_columns" not in details:
        return ""
    start, target, shared, columns = _shared(details)
    if not shared:
        return f"'{start}' and '{target}' share no key column"
    if details.get("shared_key_unique_on_target"):
        return f"'{start}' holds the key of '{target}' ({columns})"
    short = (
        f"'{target}' has no key"
        if details.get("target_row_grain")
        else f"not the whole key of '{target}'"
    )
    return (
        f"'{start}' and '{target}' share {columns}, but {short}, so one '{start}' row can match "
        f"many '{target}' rows on {_them(shared)}"
    )


def shared_key_split(details: dict[str, Any]) -> str:
    """What separate calls for each side can be matched on (for the caller's split hint)."""
    if "shared_key_columns" not in details:
        return ""
    start, target, shared, columns = _shared(details)
    if not shared:
        return (
            f" '{start}' and '{target}' share no key column, so their separate results have no "
            "key to be matched on."
        )
    if details.get("shared_key_unique_on_target"):
        return (
            f" Key columns they share: {columns}; each '{start}' row matches at most one "
            f"'{target}' row on {_them(shared)}."
        )
    return (
        f" Key columns they share: {columns}, not the whole key of '{target}', so one "
        f"'{start}' row can match many '{target}' rows on {_them(shared)}: a total across the "
        f"matched rows counts a '{start}' row once per '{target}' row it matches."
    )


def shared_key_fix(details: dict[str, Any]) -> str:
    """The package author's change when the pair shares key columns that aren't all of the
    target's key. Which relationship joins them on those depends on rows the engine doesn't
    read: many-to-one when no two target rows hold the same values, else many-to-many, which a
    call can't group or filter through."""
    start, target, shared, columns = _shared(details)
    if not shared or details.get("shared_key_unique_on_target"):
        return ""
    return (
        f"'{start}' and '{target}' share {columns}, not the whole key of '{target}'. A "
        f"graph.relationships row from '{start}' to '{target}' with `via: [{columns}]` and "
        f"`target: [{columns}]` is `cardinality: many_to_one` when no two '{target}' rows hold "
        f"the same {columns} (or, for versions, none valid at the same time, with their window "
        f"in `temporal_validity:`): each '{start}' row then meets one '{target}' row. Otherwise "
        f"it is `cardinality: many_to_many`, and a call can't group or filter a measure of "
        f"either by the other's dimensions through it: it is refused with MIXED_GRAIN_INVALID."
    )


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


# --- The same measure on another model ---------------------------------------------------------

_LOOKALIKES_OFF: ContextVar[bool] = ContextVar("lookalikes_off", default=False)
# The call keys that read a measure's value; ``where`` (dimensions, entity filters) never swaps.
_VALUE_KEYS = ("select", "metric_filters", "order_by")


@contextmanager
def without_lookalikes() -> Iterator[None]:
    """Refusals raised inside name no look-alike measures (``plan`` validating its own drafts)."""
    token = _LOOKALIKES_OFF.set(True)
    try:
        yield
    finally:
        _LOOKALIKES_OFF.reset(token)


def lookalikes_off() -> bool:
    return _LOOKALIKES_OFF.get()


def _measure_names(measure: Any, entity: Any) -> tuple[str, str]:
    """A measure's key (its name's last part) and label, each without its model's table: a
    ``<table>_`` key prefix (``rooms_hist_area``) or a ``(<table>)`` label suffix (``Area
    (ROOMS_HIST)``), as a package mirrored from warehouse tables names a column of each."""
    table = str(getattr(entity, "table", "") or "").rpartition(".")[2].casefold()
    key = str(measure.name or measure.id).rpartition(".")[2].casefold()
    prefix = re.sub(r"\W+", "_", table) + "_"
    if table and key.startswith(prefix) and len(key) > len(prefix):
        key = key[len(prefix) :]
    label = str(measure.label or "").strip()
    tagged = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", label)
    if table and tagged and tagged.group(2).strip().casefold() == table:
        label = tagged.group(1)
    return key, label.casefold()


def _columns_read(expr: Any) -> Iterator[str]:
    if isinstance(expr, dict):
        if expr.get("kind") == "column":
            yield str(expr.get("column") or "")
        for item in expr.values():
            yield from _columns_read(item)
    elif isinstance(expr, list):
        for item in expr:
            yield from _columns_read(item)


def _alike(asked: Any, other: Any, entities: dict[str, Any]) -> bool:
    """The same key or label (each without its model's table name) over the same expression of
    its own table's columns, totalled the same way: the same default aggregation, measure
    class, additivity, value type and currency. An expression that reads no column (a row
    count's literal), or a column whose name doesn't say what it identifies (a plain ``id``,
    ``_specific``), counts its own model's rows, so it is no other model's measure."""
    asked_key, asked_label = _measure_names(asked, entities.get(asked.entity))
    other_key, other_label = _measure_names(other, entities.get(other.entity))
    named = asked_key == other_key or bool(asked_label) and asked_label == other_label
    if not named:
        return False
    expression = expr_to_dict(asked.expr)
    columns = list(_columns_read(expression))
    if not columns or not all(_specific(column) for column in columns):
        return False
    if expression != expr_to_dict(other.expr):
        return False
    return all(
        getattr(asked, field) == getattr(other, field)
        for field in (
            "default_aggregation",
            "measure_class",
            "additive",
            "value_type",
            "currency",
        )
    )


def call_measures(call: dict[str, Any]) -> list[str]:
    """The measure ids a call's values read (select, having and order_by), in order."""

    def walk(value: Any) -> Iterator[str]:
        if isinstance(value, str) and value.startswith("measure."):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    return list(dict.fromkeys(ref for key in _VALUE_KEYS for ref in walk(call.get(key))))


def lookalike_swaps(
    config: PackageConfig, measure_ids: Sequence[str], start: str
) -> list[tuple[str, dict[str, str]]]:
    """Each other entity that holds a look-alike of every one of ``measure_ids`` (the call's
    measures on ``start``), with each measure's look-alike there. A look-alike is a published
    measure on another entity with the same key or label, each without its model's table name,
    that totals the same way (``_alike``). An entity with two look-alikes of one measure has no
    one look-alike, so it is left out."""
    entities = {row.id: row for row in config.entities}
    asked = [row for row in config.measures if row.id in set(measure_ids) and row.entity == start]
    if not asked:
        return []
    found: dict[str, dict[str, list[str]]] = {}
    for other in config.measures:
        if other.entity == start or not other.publish:
            continue
        for measure in asked:
            if _alike(measure, other, entities):
                found.setdefault(other.entity, {}).setdefault(measure.id, []).append(other.id)
    wanted = {measure.id for measure in asked}
    return [
        (entity, {measure_id: ids[0] for measure_id, ids in sorted(swaps.items())})
        for entity, swaps in sorted(found.items())
        if set(swaps) == wanted and all(len(ids) == 1 for ids in swaps.values())
    ]


def measure_lookalikes(config: PackageConfig, measure_id: str) -> list[Any]:
    """The published look-alikes (``_alike``) of one measure on other entities, by id."""
    entities = {row.id: row for row in config.entities}
    asked = next((row for row in config.measures if row.id == measure_id), None)
    if asked is None:
        return []
    return sorted(
        (
            other
            for other in config.measures
            if other.entity != asked.entity and other.publish and _alike(asked, other, entities)
        ),
        key=lambda row: row.id,
    )


def renamed_columns(call: dict[str, Any], swaps: dict[str, str]) -> dict[str, str]:
    """The call's result columns named by default after a swapped measure (its id's last part),
    each to its look-alike's name: what an order_by or having naming the column must say."""
    names = {old.rpartition(".")[2]: new.rpartition(".")[2] for old, new in swaps.items()}
    return {
        item["as"]: names[item["as"]]
        for item in call.get("select") or []
        if isinstance(item, dict) and item.get("as") in names
    }


def swapped_call(
    call: dict[str, Any], swaps: dict[str, str], temporal_role: str = ""
) -> dict[str, Any]:
    """``call`` (the engine's form) reading each swapped measure's look-alike in its place, in
    its values only; its group_by, where and time stay as they are, but for ``temporal_role``.
    A column named by default after a swapped measure (its id's last part) is named after its
    look-alike, as the call would name it, and so is an order_by of that name."""

    def swap(value: Any) -> Any:
        if isinstance(value, str):
            return swaps.get(value, value)
        if isinstance(value, dict):
            return {key: swap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [swap(item) for item in value]
        return value

    names = renamed_columns(call, swaps)
    out = {key: value for key, value in call.items() if key != "_call_notes"}
    out.update({key: swap(call[key]) for key in _VALUE_KEYS if key in call})
    out["select"] = [
        {**item, "as": names.get(item["as"], item["as"])}
        if isinstance(item, dict) and isinstance(item.get("as"), str)
        else item
        for item in out.get("select") or []
    ]
    if out.get("order_by"):
        out["order_by"] = [
            {**item, "field": names.get(item["field"], item["field"])}
            if isinstance(item, dict) and isinstance(item.get("field"), str)
            else item
            for item in out["order_by"]
        ]
    if temporal_role:
        out["time"] = {**dict(call.get("time") or {}), "temporal_role": temporal_role}
    return out


def model_population(entity: Any) -> str:
    """What an entity's rows are, as its author states it: its model's description, else its own
    authored description (one that isn't just its label). Empty when neither says."""
    if entity.model_description:
        return str(entity.model_description)
    return "" if entity.description in ("", entity.label) else str(entity.description)


def first_sentence(text: str) -> str:
    """A description's first sentence (through its first full stop), or all of it."""
    text = " ".join(str(text).split())
    head, stop, _ = text.partition(". ")
    return f"{head}." if stop else text


def lookalike_rows_in_view(config: PackageConfig, rows: Sequence[Any]) -> list[dict[str, Any]]:
    """The look-alike rows whose entity and measures ``config`` (a caller's view) shows, each
    with the first sentence of its model's description as this view reads it (its card has the
    rest)."""
    entities = {row.id: row for row in config.entities}
    measures = {row.id for row in config.measures}
    kept = []
    for row in rows:
        if not isinstance(row, dict) or row.get("entity") not in entities:
            continue
        pairs = dict(row.get("measures") or {})
        if not pairs or not set(pairs) | set(pairs.values()) <= measures:
            continue
        out: dict[str, Any] = {"entity": row["entity"], "measures": pairs}
        population = first_sentence(model_population(entities[row["entity"]]))
        if population:
            out["description"] = population
        for key in ("columns", "temporal_role"):
            if row.get(key):
                out[key] = row[key]
        kept.append(out)
    return kept


def _lookalike_clause(row: dict[str, Any]) -> str:
    pairs = ", ".join(f"'{new}' in place of '{old}'" for old, new in row["measures"].items())
    clock = f", by '{row['temporal_role']}'" if row.get("temporal_role") else ""
    return f"{pairs} (on '{row['entity']}'{clock})"


def lookalike_fact(rows: Sequence[dict[str, Any]]) -> str:
    """The refusal's sentence saying how many look-alikes answer the call, or ""."""
    if not rows:
        return ""
    if len(rows) == 1:
        return (
            "Another model's measure of the same name answers this call as it stands "
            "(details.lookalike_measures)."
        )
    return (
        f"{len(rows)} other models' measures of the same name answer this call as it stands "
        "(details.lookalike_measures)."
    )


def lookalike_advice(rows: Sequence[dict[str, Any]]) -> str:
    """The caller hint: each look-alike by id, how to read it, and the caveat. What each one's
    model holds is in ``details.lookalike_measures`` and, in full, on the measure's card."""
    renames = (
        " Where the call names a result column (order_by, having), use the row's `columns`."
        if any(row.get("columns") for row in rows)
        else ""
    )
    return (
        "Read the same measure from another model in details.lookalike_measures, keeping this "
        "call's group_by, where and time: "
        + "; ".join(_lookalike_clause(row) for row in rows)
        + "."
        + renames
        + " Each model holds its own rows, which may be fewer, more or other rows (such as "
        "every version of each row) than the user asked about: inspect the measure for its "
        "model's description, and tell the user which model the answer reads."
    )
