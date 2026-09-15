"""Framework-neutral semantic validation-contract producer.

This module is the single authority for converting a Semantic Rails project
into the semantic half of the public validation contract.  Integrations must
not parse project YAML independently: they consume this output and add a
framework-owned ``binding`` section.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from semantic_rails import __version__
from semantic_rails.config import LoadedPackageSnapshot, load_package_snapshot
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import expr_to_dict, parse_config_expression

CONTRACT_FORMAT_VERSION = 1
PRODUCER_NAME = "semantic-rails"

_DIMENSION_TYPES = {
    "boolean": "boolean",
    "categorical": "string",
    "continuous": "number",
    "currency": "number",
    "date": "date",
    "datetime": "timestamp",
    "integer": "integer",
    "number": "number",
    "percent": "number",
    "string": "string",
    "timestamp": "timestamp",
}


def _load_validated_snapshot(path: str | Path | LoadedPackageSnapshot) -> LoadedPackageSnapshot:
    try:
        return load_package_snapshot(path)
    except SemanticLayerError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        # Invalid cross-references can otherwise leak a raw loader exception
        # through the public producer boundary. Preserve the engine's
        # structured-error contract without echoing potentially sensitive
        # project contents from the original exception.
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Semantic contract export could not load the project.",
            details={"exception_type": type(exc).__name__},
        ) from exc


def semantic_contract_fingerprint(path: str | Path | LoadedPackageSnapshot) -> str:
    """Return the engine-owned semantic fingerprint for a project.

    The digest is SHA-256 over canonical JSON of the *loaded* PackageConfig,
    excluding connection, seed, and local database locators. It therefore
    tracks semantic models, expressions, policies, relationships, and other
    behavior while remaining stable across file layout and deployment-only
    configuration changes.
    """

    return _load_validated_snapshot(path).semantic_fingerprint


def _expression_columns(raw: Any) -> set[str]:
    if raw in (None, ""):
        return set()
    parsed = expr_to_dict(parse_config_expression(raw))
    columns: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if str(value.get("kind", "")) == "column":
                column = str(value.get("column", "") or "").strip()
                if column:
                    columns.add(column.rsplit(".", 1)[-1])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(parsed)
    return columns


def _physical_columns(raw: Any) -> set[str]:
    """Return physical column references without stringifying AST nodes.

    Key authoring accepts both scalar/list shorthand and structured
    expressions. Treat every value as an expression source so mappings,
    functions, and literals can never become invented physical column names.
    """

    if raw in (None, ""):
        return set()
    if isinstance(raw, (list, tuple, set)):
        columns: set[str] = set()
        for item in raw:
            columns.update(_physical_columns(item))
        return columns
    if isinstance(raw, str) and raw.lstrip().startswith(("{", "[")):
        # Older normalization paths can preserve a structured expression as
        # its Python-literal string. Recover only safe literals, then traverse
        # the expression AST; never treat the serialized mapping itself as a
        # column name.
        try:
            recovered = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return set()
        if isinstance(recovered, (Mapping, list, tuple, set)):
            return _physical_columns(recovered)
    return _expression_columns(raw)


def _add_column(
    columns: dict[str, dict[str, Any]],
    name: Any,
    *,
    required_by: str,
    data_type: str = "",
) -> None:
    clean = str(name or "").strip().rsplit(".", 1)[-1]
    if not clean or clean == "*":
        return
    row = columns.setdefault(clean, {"required_by": set(), "data_types": set()})
    if required_by:
        row["required_by"].add(required_by)
    if data_type:
        row["data_types"].add(data_type)


def _resource_columns(
    model_id: str,
    model: Mapping[str, Any],
    *,
    authored_model: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    columns: dict[str, dict[str, Any]] = {}
    keys = dict(model.get("keys", {}) or {})
    authored = dict(authored_model or {})
    authored_keys = dict(authored.get("keys", {}) or {})
    entity_name = str(model.get("entity", "") or "").strip()
    primary_source = authored_keys.get("primary", authored.get("grain", keys.get("primary")))
    for key in _physical_columns(primary_source):
        _add_column(
            columns,
            key,
            required_by=f"entity.{entity_name}" if entity_name else f"model.{model_id}",
        )
    foreign_sources = dict(keys.get("foreign", {}) or {})
    foreign_sources.update(dict(authored_keys.get("foreign", {}) or {}))
    for foreign_entity, entity_spec_raw in dict(authored.get("entities", {}) or {}).items():
        if str(foreign_entity) in {"bridge", entity_name} or not isinstance(
            entity_spec_raw, Mapping
        ):
            continue
        entity_spec = dict(entity_spec_raw)
        if entity_spec.get("expr") not in (None, ""):
            foreign_sources[str(foreign_entity)] = entity_spec["expr"]
    for foreign_entity, raw_columns in foreign_sources.items():
        for key in _physical_columns(raw_columns):
            _add_column(columns, key, required_by=f"entity.{foreign_entity}")

    authored_times = dict(authored.get("times", {}) or {})
    for time_name, raw_spec in dict(model.get("times", {}) or {}).items():
        spec = {
            **dict(raw_spec or {}),
            **dict(authored_times.get(time_name, {}) or {}),
        }
        object_id = str(
            spec.get("id") or spec.get("dimension_id") or f"time.{model_id}.{time_name}"
        )
        explicit_source = spec.get("column") if "column" in spec else spec.get("expr")
        physical = (
            _physical_columns(explicit_source)
            if explicit_source not in (None, "")
            else {str(time_name)}
        )
        for column in physical:
            _add_column(
                columns,
                column,
                required_by=object_id,
                data_type=_DIMENSION_TYPES.get(str(spec.get("kind", "")).lower(), ""),
            )

    authored_dimensions = dict(authored.get("dimensions", {}) or {})
    for dimension_name, raw_spec in dict(model.get("dimensions", {}) or {}).items():
        spec = {
            **dict(raw_spec or {}),
            **dict(authored_dimensions.get(dimension_name, {}) or {}),
        }
        object_id = str(spec.get("id") or f"dimension.{model_id}.{dimension_name}")
        explicit_source = spec.get("column") if "column" in spec else spec.get("expr")
        physical = (
            _physical_columns(explicit_source)
            if explicit_source not in (None, "")
            else {str(dimension_name)}
        )
        for column in physical:
            _add_column(
                columns,
                column,
                required_by=object_id,
                data_type=_DIMENSION_TYPES.get(str(spec.get("kind", "")).lower(), ""),
            )

    authored_measures = dict(authored.get("measures", {}) or {})
    for measure_name, raw_spec in dict(model.get("measures", {}) or {}).items():
        spec = {
            **dict(raw_spec or {}),
            **dict(authored_measures.get(measure_name, {}) or {}),
        }
        object_id = str(spec.get("id") or f"measure.{model_id}.{measure_name}")
        for column in _physical_columns(spec.get("entity_key")):
            _add_column(columns, column, required_by=object_id)
        for column in _expression_columns(spec.get("expr")):
            _add_column(columns, column, required_by=object_id)

    out: list[dict[str, Any]] = []
    for name, state in sorted(columns.items()):
        row: dict[str, Any] = {
            "name": name,
            "required_by": sorted(state["required_by"]),
        }
        data_types = sorted(state["data_types"])
        if len(data_types) == 1:
            row["data_type"] = data_types[0]
        out.append(row)
    return out


def _resources_from_normalized(
    normalized: Mapping[str, Any],
    *,
    authored: Mapping[str, Any],
    fallback_config: Any,
) -> list[dict[str, Any]]:
    models = dict(normalized.get("models", {}) or {})
    authored_models = dict(authored.get("models", {}) or {})
    resources: list[dict[str, Any]] = []
    for authored_name, raw_model in sorted(models.items()):
        model = dict(raw_model or {})
        model_id = str(model.get("id") or authored_name).strip()
        if not model_id:
            continue
        row: dict[str, Any] = {
            "semantic_model_id": model_id,
            "columns": _resource_columns(
                model_id,
                model,
                authored_model=dict(authored_models.get(authored_name, {}) or {}),
            ),
        }
        relation = str(model.get("relation", "") or "").strip()
        if relation:
            row["relation"] = relation
        resources.append(row)
    if resources:
        return resources

    # Legacy graph-first single-file packages do not necessarily have an
    # authored ``models`` mapping. Preserve support with a typed-loader
    # fallback grouped by entity table.
    dimensions_by_entity: dict[str, list[Any]] = {}
    measures_by_entity: dict[str, list[Any]] = {}
    for dimension in fallback_config.dimensions:
        dimensions_by_entity.setdefault(dimension.entity, []).append(dimension)
    for measure in fallback_config.measures:
        measures_by_entity.setdefault(measure.entity, []).append(measure)
    for entity in sorted(fallback_config.entities, key=lambda item: item.id):
        columns: dict[str, dict[str, Any]] = {}
        for key in [entity.primary_key, *entity.key, *entity.identifiers]:
            _add_column(columns, key, required_by=entity.id)
        for foreign_columns in entity.foreign_keys.values():
            for key in foreign_columns:
                _add_column(columns, key, required_by=entity.id)
        for dimension in dimensions_by_entity.get(entity.id, []):
            _add_column(
                columns,
                dimension.column,
                required_by=dimension.id,
                data_type=_DIMENSION_TYPES.get(dimension.data_type.lower(), dimension.data_type),
            )
        for measure in measures_by_entity.get(entity.id, []):
            for column in _expression_columns(expr_to_dict(measure.expr)):
                _add_column(columns, column, required_by=measure.id)
        rows = []
        for name, state in sorted(columns.items()):
            row = {"name": name, "required_by": sorted(state["required_by"])}
            data_types = sorted(state["data_types"])
            if len(data_types) == 1:
                row["data_type"] = data_types[0]
            rows.append(row)
        resources.append(
            {
                "semantic_model_id": entity.id,
                "relation": entity.table,
                "columns": rows,
            }
        )
    return resources


def export_semantic_contract(path: str | Path | LoadedPackageSnapshot) -> dict[str, Any]:
    """Export a framework-neutral validation contract from a project.

    ``path`` may name a split project directory or a single-file package.
    The real engine loader runs first and raises the same structured config
    errors as normal runtime startup. The returned payload is deliberately
    unwrapped so adapters can add ``binding`` without translating an
    engine-specific envelope.
    """

    snapshot = _load_validated_snapshot(path)
    config = snapshot.config
    authored = snapshot.authored
    normalized = snapshot.normalized
    package_raw = dict(normalized.get("package", {}) or {})
    namespace = str(
        package_raw.get("namespace") or package_raw.get("id") or config.package.package_id
    ).strip()
    package = {
        "package_id": config.package.package_id,
        "namespace": namespace,
        "package_schema_version": config.version,
        "semantic_hash": snapshot.semantic_fingerprint,
        "resources": _resources_from_normalized(
            normalized,
            authored=authored,
            fallback_config=config,
        ),
    }
    return {
        "contract_format_version": CONTRACT_FORMAT_VERSION,
        "semantic": {
            "producer": {
                "name": PRODUCER_NAME,
                "version": __version__,
            },
            "packages": [package],
        },
    }
