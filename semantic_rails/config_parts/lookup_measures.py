"""``kind: lookup`` measures: a parent's measure total carried onto each of its child rows.

The author writes ``from`` (a measure) and ``via`` (an entity); the loader derives the rest
and refuses a declaration whose total could be added across parents or reached two ways.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..errors import SemanticLayerError
from ..expressions import ColumnRefExpr
from ..schema import (
    EntityConfig,
    MeasureConfig,
    PackageConfig,
    PathPreferenceConfig,
    RelationshipConfig,
)

# Taken from the source or fixed: one total per parent, never re-aggregated.
_FIXED = frozenset(
    [
        "expr",
        "aggregation",
        "default_agg",
        "disallowed_aggregations",
        "suggested_aggregations",
        "additive",
        "accumulation",
        "snapshot_policy",
        "entity_key",
        "value_type",
        "currency",
        "rollup",
    ]
)


def _invalid(label: str, key: str, problem: str) -> SemanticLayerError:
    return SemanticLayerError("INVALID_CONFIG", f"{label} {key}: {problem}", details={"key": key})


def lookup_measure_spec(raw: dict[str, Any], spec: dict[str, Any], label: str) -> dict[str, Any]:
    """The loader spec of a lookup (sum only, flow); any other measure's spec unchanged."""
    if str(spec.get("kind", "")).strip().lower() != "lookup":
        if misplaced := sorted({"from", "via"} & raw.keys()):
            raise _invalid(label, misplaced[0], "only a kind: lookup measure takes from and via")
        return spec
    if fixed := sorted(_FIXED & raw.keys()):
        raise _invalid(label, fixed[0], "a lookup takes only from and via; the rest is derived")
    for key in ("from", "via"):
        if not str(raw.get(key) or "").strip():
            raise _invalid(label, key, "a lookup needs from (a measure) and via (an entity)")
    return {
        **{key: value for key, value in spec.items() if key not in _FIXED},
        "default_agg": "sum",
        "disallowed_aggregations": ["avg", "min", "max", "median", "percentile"],
        "accumulation": {"kind": "flow"},
        "expr": {"kind": "literal", "value": None},  # the key to `via`, once the graph is read
    }


def _one_link(
    entity: str, via: EntityConfig, relationships: list[RelationshipConfig]
) -> RelationshipConfig | None:
    """The one direct, untimed many-to-one relationship from ``entity`` to all of ``via``'s key."""
    links = [
        rel for rel in relationships if {rel.source_entity, rel.target_entity} == {entity, via.id}
    ]
    rel = links[0] if len(links) == 1 else None
    if (
        rel is None
        or (rel.source_entity, rel.target_entity) != (entity, via.id)
        or rel.cardinality not in {"N:1", "1:1"}
        or rel.temporal_validity
        or set(rel.target_columns or [rel.target_column]) != set(via.key or [via.primary_key])
    ):
        return None
    return rel


def lookup_links(
    measure: MeasureConfig, config: PackageConfig
) -> tuple[RelationshipConfig, RelationshipConfig]:
    """A loaded lookup's relationships to ``via``: from its own entity, and from its source's."""
    via = next(entity for entity in config.entities if entity.id == measure.lookup_via)
    source = next(row for row in config.measures if row.id == measure.lookup_from)
    child = _one_link(measure.entity, via, config.relationships)
    parent = _one_link(source.entity, via, config.relationships)
    if child is None or parent is None:  # the loader proved both; a changed config must not pass
        raise _invalid(f"Lookup measure '{measure.id}'", "via", "no single route to its parent")
    return child, parent


def resolve_lookup_measures(
    measures: list[MeasureConfig],
    relationships: list[RelationshipConfig],
    entities: list[EntityConfig],
    entity_lookup: dict[str, str],
    *,
    path_preferences: list[PathPreferenceConfig],
    path: str,
) -> list[MeasureConfig]:
    """Bind each lookup's ``from`` and ``via``, or refuse the package (``INVALID_CONFIG``)."""
    resolved: list[MeasureConfig] = []
    for measure in measures:
        if not measure.lookup_from:
            resolved.append(measure)
            continue
        label = f"{path}: lookup measure '{measure.id}'"
        ref = measure.lookup_from
        sources = [row for row in measures if ref in {row.id, row.name, row.id.split(".", 2)[-1]}]
        source = sources[0] if len(sources) == 1 else None
        if source is None or source.lookup_from or not source.additive:
            raise _invalid(
                label, "from", f"'{ref}' must name one measure: not a lookup or additive: false"
            )
        if source.measure_class != "additive" or source.accumulation.kind == "stock":
            raise _invalid(
                label, "from", f"'{ref}' is a stock or an entity count, not a sum of rows"
            )
        via_id = entity_lookup.get(measure.lookup_via, measure.lookup_via)
        via = next((entity for entity in entities if entity.id == via_id), None)
        if via is None or via.kind == "time":
            raise _invalid(label, "via", f"'{measure.lookup_via}' must name a non-time entity")
        if len(via.key or [via.primary_key]) != 1:
            raise _invalid(
                label,
                "via",
                f"'{via.id}' has a composite key {via.key}; lookup needs one key column",
            )
        child = _one_link(measure.entity, via, relationships)
        parent = _one_link(source.entity, via, relationships)
        if child is None or parent is None:
            raise _invalid(
                label,
                "via",
                f"'{measure.entity}' and '{source.entity}' each need exactly one direct, untimed "
                f"many-to-one relationship to the whole key of '{via.id}'",
            )
        for link in (child, parent):
            for route in path_preferences:
                if (route.source_entity, route.target_entity) == (
                    link.source_entity,
                    via.id,
                ) and route.relationship_path != [link.id]:
                    raise _invalid(
                        label,
                        "via",
                        f"graph.path_preferences route '{link.source_entity}' -> '{via.id}' "
                        f"({', '.join(route.relationship_path)}) conflicts with the lookup's "
                        f"direct relationship '{link.id}'",
                    )
        resolved.append(
            replace(
                measure,
                expr=ColumnRefExpr(column=(child.source_columns or [child.source_column])[0]),
                additive=False,
                value_type=source.value_type,
                currency=source.currency,
                lookup_from=source.id,
                lookup_via=via.id,
            )
        )
    return resolved
