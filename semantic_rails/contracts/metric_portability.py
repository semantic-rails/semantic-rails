"""Read-only BI projection of the engine's loaded semantics, never another compiler."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from semantic_rails import __version__
from semantic_rails.config import LoadedPackageSnapshot, load_package_snapshot
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import expr_to_dict

METRIC_PORTABILITY_VERSION = 1
QUERY_IR_SCHEMA = "https://semantic-rails.com/schemas/query_ir.v1.json"
_PRESENTATION = frozenset(
    {"name", "label", "description", "aliases", "topics", "example_entries", "authoring_warnings"}
)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _definition(row: Any, expression_field: str) -> dict[str, Any]:
    definition = {key: value for key, value in asdict(row).items() if key not in _PRESENTATION}
    definition[expression_field] = expr_to_dict(getattr(row, expression_field))
    return definition


def _metric_refs(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        found = (
            {str(value["metric"])}
            if value.get("kind") == "metric" and value.get("metric")
            else set()
        )
        return found.union(*(_metric_refs(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_metric_refs(item) for item in value))
    return set()


def _dependencies(metric_id: str, definitions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    pending = [metric_id]
    found: dict[str, Any] = {}
    while pending:
        current = pending.pop()
        if current in found:
            continue
        if current not in definitions:
            raise SemanticLayerError(
                "INVALID_CONFIG", "Metric portability encountered an unresolved metric reference"
            )
        found[current] = definitions[current]
        pending.extend(sorted(_metric_refs(definitions[current])))
    return dict(sorted(found.items()))


def _context(snapshot: LoadedPackageSnapshot) -> dict[str, Any]:
    # Hash every other loaded execution input conservatively. A supporting
    # relation/policy/measure change may affect more than one metric. Display
    # fields are removed only at semantic-object boundaries, never recursively
    # from expressions where e.g. a function's name changes meaning.
    context = snapshot.semantic
    context.pop("metric_recipes", None)
    context["package"] = {
        key: value for key, value in context["package"].items() if key not in _PRESENTATION
    }
    for key, value in context.items():
        if isinstance(value, list):
            context[key] = [
                {field: item for field, item in row.items() if field not in _PRESENTATION}
                if isinstance(row, dict) and "id" in row
                else row
                for row in value
            ]
    return context


def export_metric_portability(
    path: str | Path | LoadedPackageSnapshot,
    *,
    import_provenance: Mapping[str, Any] | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Export metric identities, definitions and Query IR references from one snapshot.

    Identity is the pair ``(package.namespace, metric.id)``. It survives source
    movement and formatting, but explicit ID/namespace renames are breaking.
    Definition hashes are change detectors, not identity or authorization.
    This author/export artifact contains definitions, including physical
    expressions; hosted distribution requires full-package author access.
    """
    snapshot = load_package_snapshot(path)
    config = snapshot.config
    if snapshot.source_kind == "in_memory":
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("In-memory metric export requires an explicit namespace")
    else:
        derived_namespace = str(
            snapshot.normalized.get("package", {}).get("namespace") or config.package.package_id
        )
        if namespace is not None and namespace != derived_namespace:
            raise ValueError("Metric namespace must match the authored package")
        namespace = derived_namespace
    context_hash = _hash(_context(snapshot))
    definitions = {metric.id: _definition(metric, "expression") for metric in config.metric_recipes}
    metrics = []
    for metric in sorted(config.metric_recipes, key=lambda row: row.id):
        metrics.append(
            {
                "id": metric.id,
                "label": metric.label or metric.name or metric.id,
                "description": metric.description,
                "definition": definitions[metric.id],
                "definition_hash": _hash(
                    {"context": context_hash, "metrics": _dependencies(metric.id, definitions)}
                ),
                "query_template": {
                    "version": 1,
                    "select": [
                        {"expression": {"kind": "metric", "metric": metric.id}, "as": "value"}
                    ],
                },
            }
        )
    provenance = deepcopy(dict(import_provenance)) if import_provenance is not None else None
    if provenance is not None and (
        type(provenance.get("format_version")) is not int
        or provenance.get("format_version") != 1
        or provenance.get("framework") != "metricflow"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(provenance.get("parsed_input_hash", "")))
        or not isinstance(provenance.get("warnings"), list)
        or not all(isinstance(item, str) for item in provenance.get("warnings", []))
    ):
        raise SemanticLayerError("INVALID_CONFIG", "Unsupported metric import provenance")
    return {
        "contract_format_version": METRIC_PORTABILITY_VERSION,
        "producer": {"name": "semantic-rails", "version": __version__},
        "package": {
            "id": config.package.package_id,
            "namespace": namespace,
            "schema_version": config.version,
            "semantic_hash": snapshot.semantic_fingerprint,
        },
        "context_hash": context_hash,
        "metrics": metrics,
        "measures": [
            _definition(measure, "expr")
            for measure in sorted(config.measures, key=lambda row: row.id)
        ],
        "query_capabilities": {
            "schema_id": QUERY_IR_SCHEMA,
            "version": 1,
            "runtime_validation_required": True,
            "groupable_dimensions": sorted(row.id for row in config.dimensions if row.groupable),
            "filterable_dimensions": sorted(row.id for row in config.dimensions if row.filterable),
            "temporal_roles": [
                {
                    "id": row.id,
                    "supported_grains": list(row.supported_grains),
                    "timezone": row.timezone,
                }
                for row in sorted(config.temporal_roles, key=lambda row: row.id)
            ],
        },
        "provenance": {
            "source_fingerprint": "sha256:" + snapshot.source_fingerprint.removeprefix("sha256:"),
            "import": provenance,
        },
    }


def compare_metric_portability(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Classify changes to valid v1 exports conservatively for stored BI bindings.

    Unknown majors and duplicate identities fail closed. A metric's stored
    hash includes supporting package semantics and referenced metric recipes.
    A different engine version is a requalification requirement, not proof
    that otherwise identical query behavior remains equivalent.
    """
    indexes = []
    for payload in (before, after):
        if (
            type(payload.get("contract_format_version")) is not int
            or payload["contract_format_version"] != 1
        ):
            raise ValueError("Unsupported metric portability contract major")
        rows = payload.get("metrics")
        if not isinstance(rows, list) or not isinstance(payload.get("package"), Mapping):
            raise ValueError("Invalid metric portability contract")
        namespace = payload["package"].get("namespace")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("Missing metric namespace")
        context_hash = payload.get("context_hash")
        if not isinstance(context_hash, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", context_hash
        ):
            raise ValueError("Invalid metric context hash")
        index = {}
        definitions = {}
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
                raise ValueError("Invalid metric portability identity")
            key = (namespace, row["id"])
            if key in index:
                raise ValueError("Duplicate metric portability identity")
            if (
                not isinstance(row.get("definition"), dict)
                or row["definition"].get("id") != row["id"]
            ):
                raise ValueError("Metric definition does not match identity")
            expected_query = {
                "version": 1,
                "select": [{"expression": {"kind": "metric", "metric": row["id"]}, "as": "value"}],
            }
            if row.get("query_template") != expected_query:
                raise ValueError("Metric query template does not match identity")
            index[key] = row
            definitions[row["id"]] = row["definition"]
        for row in rows:
            expected_hash = _hash(
                {"context": context_hash, "metrics": _dependencies(row["id"], definitions)}
            )
            if row.get("definition_hash") != expected_hash:
                raise ValueError("Metric definition hash does not match definitions")
        indexes.append(index)
    previous, current = indexes
    changes = []
    for key in sorted(previous.keys() | current.keys()):
        if key not in previous:
            kind, code = "additive", "METRIC_ADDED"
        elif key not in current:
            kind, code = "breaking", "METRIC_REMOVED"
        elif previous[key]["definition_hash"] != current[key]["definition_hash"]:
            kind, code = "breaking", "METRIC_DEFINITION_CHANGED"
        elif any(
            previous[key].get(field) != current[key].get(field)
            for field in ("label", "description")
        ):
            kind, code = "metadata", "METRIC_PRESENTATION_CHANGED"
        else:
            continue
        changes.append({"kind": kind, "code": code, "namespace": key[0], "metric_id": key[1]})
    if before.get("measures") != after.get("measures"):
        changes.append({"kind": "breaking", "code": "SUPPORTING_DEFINITIONS_CHANGED"})
    if before.get("query_capabilities") != after.get("query_capabilities"):
        changes.append({"kind": "breaking", "code": "QUERY_CAPABILITIES_CHANGED"})
    if before.get("producer") != after.get("producer"):
        changes.append({"kind": "breaking", "code": "ENGINE_REQUALIFICATION_REQUIRED"})
    if before.get("provenance") != after.get("provenance"):
        changes.append({"kind": "metadata", "code": "SOURCE_PROVENANCE_CHANGED"})
    classification = next(
        (
            kind
            for kind in ("breaking", "additive", "metadata")
            if any(row["kind"] == kind for row in changes)
        ),
        "none",
    )
    return {
        "report_format_version": 1,
        "classification": classification,
        "compatible": classification != "breaking",
        "changes": changes,
    }
