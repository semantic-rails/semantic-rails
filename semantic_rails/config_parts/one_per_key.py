"""One-per-key models: a relation with no key, read as one row per its entity's key.

``one_per_key: true`` on a model lowers to a relation pipeline over the model's relation:
``source → where every key column is set → group_by the key``, each other column it reads
aggregated with ``single_value`` (the key's one value, NULL where its rows hold two). Lookups
into it are ordinary N:1 joins, unique by construction. A row-grain model over the same relation,
with a foreign key on that key, is its *link*: walked in reverse it reaches the columns that can
hold several values per key.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from ..errors import SemanticLayerError
from ..expressions import collect_column_refs
from ..naming import slug
from ..schema import (
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    PackageConfig,
    RelationConfig,
    RelationPipelineStep,
    RelationshipConfig,
)

# Every name a one-per-key relation and its steps are read under starts with this, and no
# authored name may hold its stem (check_one_per_key_names).
RESERVED = "sr_one_per_key__"
_STEM = RESERVED.rstrip("_")


def one_per_key_source(config: PackageConfig, entity: EntityConfig) -> str:
    """The relation a one-per-key model names (its pipeline's source), else ""."""
    relation = next((row for row in config.relations if row.id == entity.relation_id), None)
    if not entity.one_per_key or relation is None:
        return ""
    return str(relation.steps[0].config.get("relation", ""))


def _columns_read(
    entity: EntityConfig,
    dimensions: list[DimensionConfig],
    measures: list[MeasureConfig],
    relationships: list[RelationshipConfig],
) -> list[str]:
    """The relation columns the model reads besides its key, in first-read order: its foreign
    keys, its dimensions, its measures' operands and the columns a join to or from it reads."""
    columns = [column for cols in entity.foreign_keys.values() for column in cols]
    for rel in relationships:
        if rel.source_entity == entity.id:
            columns += rel.source_columns or [rel.source_column]
        if rel.target_entity == entity.id:
            columns += rel.target_columns or [rel.target_column]
    columns += [row.column for row in dimensions if row.entity == entity.id and row.column]
    for measure in measures:
        if measure.entity == entity.id:
            columns += [
                ref.column
                for ref in collect_column_refs(measure.expr)
                if ref.entity in {"", entity.id} and not ref.table
            ]
    key = set(entity.key_columns)
    return list(dict.fromkeys(column for column in columns if column not in key))


def one_per_key_relation(
    entity: EntityConfig,
    source: str,
    dimensions: list[DimensionConfig],
    measures: list[MeasureConfig],
    relationships: list[RelationshipConfig],
) -> RelationConfig:
    """The relation pipeline that reads ``source`` as one row per ``entity``'s key, named after
    the entity (an id a rewritten package keeps)."""
    key = list(entity.key_columns)
    columns = _columns_read(entity, dimensions, measures, relationships)
    # Some warehouses take no MIN or MAX of a true/false column (dialects.single_value).
    booleans = {
        row.column for row in dimensions if row.entity == entity.id and row.data_type == "boolean"
    }
    name = RESERVED + slug(entity.id.removeprefix("entity."))
    return RelationConfig(
        id=f"relation.{name}",
        output_name=name,
        steps=[
            RelationPipelineStep("source", {"relation": source, "columns": [*key, *columns]}),
            RelationPipelineStep(
                "where",
                {"predicates": [{"field": col, "op": "missing", "value": False} for col in key]},
            ),
            RelationPipelineStep(
                "group_by",
                {
                    "dimensions": {col: col for col in key},
                    "aggregates": {
                        col: {
                            "function": "single_value",
                            "expr": col,
                            **({"boolean": True} if col in booleans else {}),
                        }
                        for col in columns
                    },
                },
            ),
        ],
        columns=[*key, *columns],
        label=entity.label,
        description=f"One row per {', '.join(key)} of {source}.",
    )


def with_one_per_key(
    entities: list[EntityConfig],
    sources: dict[str, str],
    dimensions: list[DimensionConfig],
    measures: list[MeasureConfig],
    relationships: list[RelationshipConfig],
    relations: list[RelationConfig],
) -> tuple[list[EntityConfig], list[RelationshipConfig], list[RelationConfig]]:
    """Point each one-per-key entity (``sources``: entity id → the relation its model names) at
    its one-row-per-key relation, and mark each link into one. Its names are checked once the
    package is whole (:func:`check_one_per_key_names`)."""
    if not sources:
        return entities, relationships, relations
    by_id = {row.id: row for row in entities}
    added: list[RelationConfig] = []
    for entity_id, source in sources.items():
        entity = by_id[entity_id]
        relation = one_per_key_relation(entity, source, dimensions, measures, relationships)
        added.append(relation)
        by_id[entity_id] = replace(entity, table=relation.output_name, relation_id=relation.id)

    def link(rel: RelationshipConfig) -> bool:
        source_entity, target = by_id[rel.source_entity], by_id[rel.target_entity]
        return (
            target.id in sources
            and source_entity.is_row_grain
            and source_entity.table == sources[target.id]
            and list(rel.source_columns) == list(rel.target_columns) == target.key_columns
        )

    return (
        [by_id[row.id] for row in entities],
        [replace(rel, link=True) if link(rel) else rel for rel in relationships],
        [*relations, *added],
    )


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _normalized(name: str) -> str:
    """A name as any warehouse might read it: unpadded, in one case, and with each character a
    warehouse would quote or replace (a schema dot, a dash) read as an underscore."""
    return re.sub(r"[^0-9a-z]", "_", str(name).strip().casefold())


def check_one_per_key_names(config: PackageConfig) -> None:
    """Keep the names one-per-key relations are read under apart from every authored one.

    Each generated name (the relation's and each step's) starts with :data:`RESERVED`, which a
    warehouse's identifier shortening keeps (Postgres keeps a name's first 52 bytes). No authored
    table, relation or pipeline string may hold its stem, read as :func:`_normalized` reads it.
    An authored pipeline's steps are read under its output name and a suffix, so none of them can
    start with the reserved prefix either, and no query reading an authored name can read
    one-per-key rows instead, whatever the warehouse."""
    generated = {entity.relation_id for entity in config.entities if entity.one_per_key}
    if not generated:
        return
    authored = [row.table for row in config.entities if not row.one_per_key]
    for row in config.relations:
        if row.id in generated:  # the relation its model names
            authored.append(str(row.steps[0].config.get("relation", "")))
            continue
        authored += [row.id, row.output_name]
        authored += [name for step in row.steps for name in _strings(step.config)]
    authored += [row.relation for row in config.aggregate_relations]
    authored += [row.zero_through.relation for row in config.entities if row.zero_through]
    for name in authored:
        if _STEM in _normalized(name):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"The table or relation name '{name}' holds '{_STEM}', but {_STEM} is reserved "
                "for the names one-per-key models' relations are read under: a query could read "
                "one as the other. Rename it.",
                details={"name": name},
            )
    outputs = [_normalized(row.output_name) for row in config.relations if row.id in generated]
    if repeated := sorted({name for name in outputs if outputs.count(name) > 1}):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Two one-per-key models' relations would be read under the name '{repeated[0]}': "
            "give their entities ids that differ in more than case and punctuation.",
            details={"name": repeated[0]},
        )
