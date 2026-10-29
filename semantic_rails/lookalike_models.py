"""Models an agent can read for one another, which the package author decides between.

An agent picks a source on purpose: a history copy of a table, or one of several tables that
count the same thing under one name, answers its question as asked, with another population.
Only the author knows which one a question means, so validation names each group of
look-alike models once (``LOOKALIKE_MODELS``) and asks which one is canonical.

Two kinds of group, read from the YAML alone, never from labels or descriptions:

- **Same rows** (``same_rows``): models whose rows are the same things, as the look-alike
  measure rule reads them (``path_gap._Grains.same_things``): keyed by the same key, or by
  that key plus time columns of its own (a history or snapshot); or keyless, one reading every
  column the other does. A calendar (``kind: time``) is the package's time axis, which every
  date-keyed model shares by design, so it is in no group.
- **Same count** (``same_count``): models whose rows are not the same things, each with a
  published count of the same name over the same column, counted the same way
  (``path_gap._alike``; its unit is the count card's ``count_unit``). Each counts the column's
  values across its own rows, so each answers "how many" for another population.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .expressions import expr_to_dict
from .metadata_parts.guidance import _count_unit
from .path_gap import _alike, _Grains, _measure_names
from .schema import PackageConfig

LOOKALIKE_MODELS = "LOOKALIKE_MODELS"
# A warning's message names this many models; its details name all of them.
_NAMED = 5


@dataclass(frozen=True)
class LookalikeGroup:
    """One group of look-alike models (entity ids, sorted) and what makes them alike."""

    kind: str  # "same_rows" or "same_count"
    entities: tuple[str, ...]
    # same_rows: each model's key, and for keyless ones the columns its dimensions read.
    keys: dict[str, list[str]] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)
    # same_count: each model's look-alike counts, and the unit and name they share.
    measures: dict[str, list[str]] = field(default_factory=dict)
    unit: str = ""
    name: str = ""


def _components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """The connected components of two or more nodes, each sorted, in order of their first."""
    parent = {node: node for node in nodes}

    def root(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in edges:
        a, b = root(left), root(right)
        if a != b:
            parent[max(a, b)] = min(a, b)
    groups: dict[str, list[str]] = defaultdict(list)
    for node in sorted(parent):
        groups[root(node)].append(node)
    return sorted((rows for rows in groups.values() if len(rows) > 1), key=lambda rows: rows[0])


def _unit(measure: Any) -> str:
    """A count's unit without its own model: ``distinct term_code``, ``rows``."""
    unit = str(_count_unit(measure).get("count_unit", ""))
    return unit.replace(f" across rows of {measure.entity}", "").replace(
        f"rows of {measure.entity}", "rows"
    )


def _same_rows_pairs(grains: _Grains, entities: dict[str, Any]) -> set[tuple[str, str]]:
    """Each pair of ``entities`` whose rows are the same things (``_Grains.same_things``), by
    sorted ids. Either pair's smaller key (or, keyless, its fewer columns) lies within the
    other's, so each entity is compared only with those holding its first such column."""
    columns = {
        entity: sorted(grains.columns.key_of(entity) or grains.described.get(entity, set()))
        for entity in entities
    }
    holding: dict[tuple[bool, str], list[str]] = defaultdict(list)
    for entity, held in columns.items():
        for column in held:
            holding[(bool(entities[entity].key_columns), column)].append(entity)
    pairs: set[tuple[str, str]] = set()
    for entity, held in columns.items():
        if not held:
            continue
        for other in holding[(bool(entities[entity].key_columns), held[0])]:
            pair = (min(entity, other), max(entity, other))
            if other != entity and pair not in pairs and grains.same_things(*pair):
                pairs.add(pair)
    return pairs


def _count_signature(measure: Any) -> str:
    """What two counts that can be alike share: their expression and how they are totalled."""
    fields = ("default_aggregation", "measure_class", "additive", "value_type", "currency")
    signature = [expr_to_dict(measure.expr), *(getattr(measure, name) for name in fields)]
    return json.dumps(signature, sort_keys=True, default=str)


def lookalike_groups(config: PackageConfig) -> list[LookalikeGroup]:
    """Every group of look-alike models in ``config``: the same-rows groups, then the
    same-count ones, each in order of its first model."""
    grains = _Grains(config)
    entities = {row.id: row for row in config.entities if row.kind != "time"}
    same_rows = _components(entities, _same_rows_pairs(grains, entities))
    groups = [
        LookalikeGroup(
            kind="same_rows",
            entities=tuple(rows),
            keys={entity: list(entities[entity].key_columns) for entity in rows},
            columns={
                entity: sorted(grains.described.get(entity, set()))
                for entity in rows
                if not entities[entity].key_columns
            },
        )
        for rows in same_rows
    ]
    # Two counts on models of one same-rows group are that group's: never a count group too.
    group_of = {entity: index for index, rows in enumerate(same_rows) for entity in rows}
    buckets: dict[str, list[Any]] = defaultdict(list)
    for measure in config.measures:
        if measure.publish and measure.entity in entities and _count_unit(measure):
            buckets[_count_signature(measure)].append(measure)
    edges: list[tuple[str, str]] = []
    by_id: dict[str, Any] = {}
    for rows in buckets.values():
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                if left.entity == right.entity or group_of.get(left.entity, -1) == group_of.get(
                    right.entity, -2
                ):
                    continue
                if _alike(left, right, entities):
                    edges.append((left.id, right.id))
                    by_id.update({left.id: left, right.id: right})
    for measure_ids in _components(by_id, edges):
        held: dict[str, list[str]] = defaultdict(list)
        for measure_id in measure_ids:
            held[by_id[measure_id].entity].append(measure_id)
        first = by_id[measure_ids[0]]
        groups.append(
            LookalikeGroup(
                kind="same_count",
                entities=tuple(sorted(held)),
                measures={entity: held[entity] for entity in sorted(held)},
                unit=_unit(first),
                name=_measure_names(first, entities.get(first.entity))[0],
            )
        )
    return groups


def _listed(items: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``; past ``_NAMED`` items, ``a, ..., e and 3 more``."""
    shown = items[:_NAMED]
    rest = len(items) - len(shown)
    if rest:
        return f"{', '.join(shown)} and {rest} more"
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def _same_rows_basis(group: LookalikeGroup) -> str:
    """Why the group's rows are the same things, as one parenthesis."""
    keys = [group.keys[entity] for entity in group.entities]
    if not any(keys):
        if len(group.entities) > 2:
            return "none has a key, and each reads every column of another, or another all of its"
        left, right = group.entities
        if set(group.columns[left]) == set(group.columns[right]):
            return "neither has a key, and both read the same columns"
        wide, narrow = (
            (left, right)
            if set(group.columns[right]) <= set(group.columns[left])
            else (right, left)
        )
        return f"neither has a key, and {wide} reads every column {narrow} does"
    common = [column for column in keys[0] if all(column in key for key in keys)]
    adds: dict[str, list[str]] = defaultdict(list)
    for entity, key in zip(group.entities, keys, strict=True):
        if extra := [column for column in key if column not in common]:
            adds[", ".join(extra)].append(entity)
    added = [
        f"{_listed(rows)} add{'s' if len(rows) == 1 else ''} {columns}"
        for columns, rows in adds.items()
    ]
    versions = f"; {'; '.join(added)}: one row per version" if added else ""
    return f"keyed by {', '.join(common)}{versions}"


def lookalike_warning(group: LookalikeGroup, source_path: Path | str) -> dict[str, Any]:
    """One ``LOOKALIKE_MODELS`` warning: the group's models and the question for the author."""
    models = list(group.entities)
    if group.kind == "same_rows":
        alike = f"hold the same rows ({_same_rows_basis(group)})"
        fix = (
            "Say what each one's rows are in its model's description, and remove a copy no "
            "question needs."
        )
        details: dict[str, Any] = {"kind": group.kind, "entities": models, "keys": group.keys}
    else:
        alike = (
            f"each count {group.unit} under one name ({group.name}) over rows that aren't the "
            "same things"
        )
        details = {
            "kind": group.kind,
            "entities": models,
            "measures": group.measures,
            "unit": group.unit,
            "name": group.name,
        }
        fix = (
            "Name each count for the rows it counts, or set publish: false on those no question "
            "needs."
        )
    return {
        "code": LOOKALIKE_MODELS,
        "severity": "warning",
        "message": (
            f"{source_path}: {_listed(models)} {alike}, so an agent can answer one question "
            f"from any of them. Which is canonical? {fix}"
        ),
        "details": details,
    }


def lookalike_warnings(config: PackageConfig, source_path: Path | str) -> list[dict[str, Any]]:
    """The ``LOOKALIKE_MODELS`` warnings of ``config``, one per group."""
    return [lookalike_warning(group, source_path) for group in lookalike_groups(config)]
