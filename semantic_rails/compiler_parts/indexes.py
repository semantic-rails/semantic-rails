from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any

from ..expressions import resolve_table_entity
from ..schema import (
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MetricConfig,
    PackageConfig,
    RelationshipConfig,
    TemporalRoleConfig,
)
from .dependencies import binding_index

GraphIndex = dict[str, list[tuple[str, str]]]


@dataclass
class PackageAnalysis:
    entities: dict[str, EntityConfig]
    dimensions: dict[str, DimensionConfig]
    measures: dict[str, MeasureConfig]
    recipes: dict[str, MetricConfig]
    temporal_roles: dict[str, TemporalRoleConfig]
    relationships: dict[str, RelationshipConfig]
    table_to_entities: dict[str, tuple[str, ...]]
    graph: GraphIndex
    path_preferences: dict[tuple[str, str], list[str]]
    temporal_relationship_ids: set[str]
    path_cache: dict[
        tuple[str, str, int, str], tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]
    ] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: PackageConfig) -> PackageAnalysis:
        tables: dict[str, set[str]] = {}
        for entity in config.entities:
            tables.setdefault(entity.table, set()).add(entity.id)
        relationships = {row.id: row for row in config.relationships}
        graph: GraphIndex = {}
        for rel in config.relationships:
            directions = {
                str(item).strip().lower()
                for item in list(rel.allowed_directions or ["forward", "reverse"])
            }
            if "forward" in directions:
                graph.setdefault(rel.source_entity, []).append((rel.target_entity, rel.id))
            if "reverse" in directions:
                graph.setdefault(rel.target_entity, []).append((rel.source_entity, rel.id))
        return cls(
            entities={row.id: row for row in config.entities},
            dimensions={row.id: row for row in config.dimensions},
            measures={row.id: row for row in config.measures},
            recipes={row.id: row for row in config.metric_recipes},
            temporal_roles={row.id: row for row in config.temporal_roles},
            relationships=relationships,
            table_to_entities={table: tuple(sorted(ids)) for table, ids in tables.items()},
            graph=graph,
            path_preferences={
                (row.source_entity, row.target_entity): list(row.relationship_path)
                for row in config.path_preferences
            },
            temporal_relationship_ids={
                row.id for row in config.relationships if row.temporal_validity
            },
        )


_PACKAGE_ANALYSIS_CACHE: dict[int, tuple[Any, PackageAnalysis]] = {}


def get_package_analysis(config: PackageConfig) -> PackageAnalysis:
    cache_key = id(config)
    cached = _PACKAGE_ANALYSIS_CACHE.get(cache_key)
    if cached is not None and cached[0]() is config:
        return cached[1]

    def _discard(_ref: Any, *, key: int = cache_key) -> None:
        _PACKAGE_ANALYSIS_CACHE.pop(key, None)

    try:
        config_ref: Any = weakref.ref(config, _discard)
    except TypeError:

        def config_ref() -> PackageConfig:
            return config

    analysis = PackageAnalysis.from_config(config)
    _PACKAGE_ANALYSIS_CACHE[cache_key] = (config_ref, analysis)
    return analysis


def _entity_index(config: PackageConfig) -> dict[str, Any]:
    return binding_index(get_package_analysis(config).entities, config)


def _dimension_index(config: PackageConfig) -> dict[str, DimensionConfig]:
    return binding_index(get_package_analysis(config).dimensions, config)


def _measure_index(config: PackageConfig) -> dict[str, MeasureConfig]:
    return binding_index(get_package_analysis(config).measures, config)


def _recipe_index(config: PackageConfig) -> dict[str, MetricConfig]:
    return binding_index(get_package_analysis(config).recipes, config)


def _temporal_role_index(config: PackageConfig) -> dict[str, TemporalRoleConfig]:
    return binding_index(get_package_analysis(config).temporal_roles, config)


def _relationship_index(config: PackageConfig) -> dict[str, RelationshipConfig]:
    return binding_index(get_package_analysis(config).relationships, config)


def _resolve_table_entity(config: PackageConfig, table: str, *, owner: str = "") -> str | None:
    return resolve_table_entity(config, table, owner=owner)


def _default_temporal_role(measure: MeasureConfig) -> str:
    return measure.compatible_temporal_roles[0] if measure.compatible_temporal_roles else ""
