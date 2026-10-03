"""``kind: lookup`` measures: a parent's measure total carried onto each of its child rows.

The author writes ``from`` (a measure) and ``via`` (an entity) and nothing else. The loader
derives the rest, and refuses any declaration whose carried value could be added across two
parents or read through more than one route.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..errors import SemanticLayerError
from ..expressions import ColumnRefExpr
from ..schema import EntityConfig, MeasureConfig, PackageConfig, RelationshipConfig

# Keys a lookup takes from its source or fixes itself: one total per parent, never re-aggregated.
_FIXED = (
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
)
_STATISTICS = ["avg", "min", "max", "median", "percentile"]


def _invalid(label: str, key: str, problem: str) -> SemanticLayerError:
    return SemanticLayerError("INVALID_CONFIG", f"{label} {key}: {problem}", details={"key": key})


def lookup_measure_spec(raw: dict[str, Any], spec: dict[str, Any], label: str) -> dict[str, Any]:
    """The loader spec of a lookup (sum only, flow); any other measure's spec unchanged."""
    if str(spec.get("kind", "")).strip().lower() != "lookup":
        for key in ("from", "via"):
            if key in raw:
                raise _invalid(label, key, "only a kind: lookup measure takes from and via")
        return spec
    for key in _FIXED:
        if key in raw:
            raise _invalid(
                label,
                key,
                "a lookup takes only from and via: its type comes from the source measure, and "
                "it is one total per parent, never re-aggregated",
            )
    for key in ("from", "via"):
        if not str(raw.get(key) or "").strip():
            raise _invalid(label, key, "a lookup needs from (a measure) and via (an entity)")
    return {
        **{key: value for key, value in spec.items() if key not in _FIXED},
        "default_agg": "sum",
        "disallowed_aggregations": _STATISTICS,
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
        or rel.source_entity != entity
        or rel.target_entity != via.id
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
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Lookup measure '{measure.id}' lost its route to its via entity"
        )
    return child, parent


def resolve_lookup_measures(
    measures: list[MeasureConfig],
    relationships: list[RelationshipConfig],
    entities: list[EntityConfig],
    entity_lookup: dict[str, str],
    *,
    path: str,
) -> list[MeasureConfig]:
    """Bind each lookup's ``from`` and ``via``, or refuse the package (``INVALID_CONFIG``)."""
    entity_by_id = {entity.id: entity for entity in entities}
    resolved: list[MeasureConfig] = []
    for measure in measures:
        if not measure.lookup_from:
            resolved.append(measure)
            continue
        label = f"{path}: lookup measure '{measure.id}'"
        ref = measure.lookup_from
        sources = [row for row in measures if ref in {row.id, row.name, row.id.split(".", 2)[-1]}]
        if len(sources) != 1:
            raise _invalid(label, "from", f"'{ref}' names no single measure")
        source = sources[0]
        if (
            source.lookup_from
            or not source.additive
            or source.measure_class != "additive"
            or source.accumulation.kind == "stock"
        ):
            raise _invalid(
                label,
                "from",
                f"'{source.id}' is a stock, an entity count, additive: false or a lookup; only "
                "a measure whose rows add up can be totalled per parent",
            )
        via = entity_by_id.get(entity_lookup.get(measure.lookup_via, measure.lookup_via))
        if via is None or via.kind == "time":
            raise _invalid(label, "via", f"'{measure.lookup_via}' must name a non-time entity")
        child = _one_link(measure.entity, via, relationships)
        if child is None or _one_link(source.entity, via, relationships) is None:
            raise _invalid(
                label,
                "via",
                f"both '{measure.entity}' and the source's '{source.entity}' need exactly one "
                f"direct, untimed many-to-one relationship to all of '{via.id}''s key",
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
