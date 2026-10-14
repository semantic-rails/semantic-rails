"""One-per-key models: a relation with no key, read as one row per its entity's key.

``one_per_key: true`` on a model lowers to a relation pipeline over the model's relation:
``source → where every key column is set → group_by the key``, each other column it reads
aggregated with ``single_value`` (the key's one value, NULL where its rows hold two). Lookups
into it are ordinary N:1 joins, unique by construction. A row-grain model over the same relation,
with a foreign key on that key, is its *link*: walked in reverse it reaches the columns that can
hold several values per key.
"""

from __future__ import annotations

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
    name = slug(entity.id.removeprefix("entity."))
    return RelationConfig(
        id=f"relation.one_per_key.{name}",
        output_name=f"{name}__one_per_key",
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


def check_one_per_key_names(config: PackageConfig) -> None:
    """Refuse a name a one-per-key model's relation is read under (or one of its steps is) that
    the package also reads or defines as another relation or table: a query reading that name
    would read the one-per-key rows instead. Names compare in any case, as most warehouses read
    an unquoted name, and a pipeline's every string counts, so nothing it reads is missed."""
    for entity in config.entities:
        relation = next((row for row in config.relations if row.id == entity.relation_id), None)
        if not entity.one_per_key or relation is None:
            continue
        others = [row.table for row in config.entities if row.id != entity.id]
        others += [
            name
            for row in config.relations
            if row is not relation
            for name in (row.id, row.output_name)
        ]
        others += [
            name for row in config.relations for step in row.steps for name in _strings(step.config)
        ]
        others += [row.relation for row in config.aggregate_relations]
        others += [row.zero_through.relation for row in config.measures if row.zero_through]
        own = relation.output_name.casefold()
        for name in others:
            folded = name.casefold()
            if folded in {own, relation.id.casefold()} or folded.startswith(own + "__"):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"'{entity.id}' is one_per_key, and its relation is read under the name "
                    f"'{relation.output_name}', which the package also reads or names as "
                    f"'{name}': a query would read one as the other. Rename that table or "
                    "relation.",
                    details={"entity": entity.id, "name": name},
                )
