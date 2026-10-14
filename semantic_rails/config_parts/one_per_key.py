"""One-per-key models: a relation with no key, read as one row per its entity's key.

``one_per_key: true`` on a model lowers to a relation pipeline over the model's relation:
``source → where every key column is set → group_by the key``, each other column it reads
aggregated with ``single_value`` (the key's one value, NULL where its rows hold two). Lookups
into it are ordinary N:1 joins, unique by construction. A row-grain model over the same relation,
with a foreign key on that key, is its *link*: walked in reverse it reaches the columns that can
hold several values per key.
"""

from __future__ import annotations

from dataclasses import replace

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
    entity: EntityConfig, dimensions: list[DimensionConfig], measures: list[MeasureConfig]
) -> list[str]:
    """The relation columns the model reads besides its key, in first-read order."""
    columns = [column for cols in entity.foreign_keys.values() for column in cols]
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
) -> RelationConfig:
    """The relation pipeline that reads ``source`` as one row per ``entity``'s key, named after
    the entity (an id a rewritten package keeps)."""
    key = list(entity.key_columns)
    columns = _columns_read(entity, dimensions, measures)
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
                        col: {"function": "single_value", "expr": col} for col in columns
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
    sources: dict[str, tuple[str, str]],
    dimensions: list[DimensionConfig],
    measures: list[MeasureConfig],
    relationships: list[RelationshipConfig],
    relations: list[RelationConfig],
) -> tuple[list[EntityConfig], list[RelationshipConfig], list[RelationConfig]]:
    """Point each one-per-key entity (``sources``: entity id → its model and the relation the
    model names) at its one-row-per-key relation, and mark each link into one."""
    if not sources:
        return entities, relationships, relations
    taken = {name for row in relations for name in (row.id, row.output_name)}
    by_id = {row.id: row for row in entities}
    added: list[RelationConfig] = []
    for entity_id, (model_id, source) in sources.items():
        entity = by_id[entity_id]
        relation = one_per_key_relation(entity, source, dimensions, measures)
        if {relation.id, relation.output_name} & taken:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"model '{model_id}' is one_per_key, and its relation would be named "
                f"'{relation.output_name}', which the package already uses",
            )
        taken |= {relation.id, relation.output_name}
        added.append(relation)
        by_id[entity_id] = replace(entity, table=relation.output_name, relation_id=relation.id)

    def link(rel: RelationshipConfig) -> bool:
        source_entity, target = by_id[rel.source_entity], by_id[rel.target_entity]
        return (
            target.id in sources
            and source_entity.is_row_grain
            and source_entity.table == sources[target.id][1]
            and list(rel.source_columns) == list(rel.target_columns) == target.key_columns
        )

    return (
        [by_id[row.id] for row in entities],
        [replace(rel, link=True) if link(rel) else rel for rel in relationships],
        [*relations, *added],
    )
