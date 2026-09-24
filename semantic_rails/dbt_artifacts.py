"""Read a dbt project's ``manifest.json`` and ``catalog.json`` for package authoring.

dbt already knows what an analytics project's relations are: where each model
lands (database, schema, alias), its columns with types and descriptions, and,
through its tests, which columns are keys (``unique`` + ``not_null``, or
``dbt_utils.unique_combination_of_columns``), which reference other models
(``relationships``) and which take a fixed set of values (``accepted_values``).
Enforced model contracts add declared ``primary_key``/``foreign_key``
constraints. This module reads those artifacts, never runs dbt, and turns each
model into the same suggestion shape as
:func:`semantic_rails.architect_introspection.suggest_model`.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .architect_introspection import (
    classify_column,
    entity_name,
    measure_aggregation,
    upsert_model_draft,
)
from .errors import SemanticLayerError

_RELATION_RESOURCES = ("model", "seed", "snapshot", "source")
_REF = re.compile(r"""ref\(\s*['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?\s*(?:,[^)]*)?\)""")
_SOURCE = re.compile(r"""source\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""")


@dataclass
class DbtColumn:
    name: str
    data_type: str = ""
    description: str = ""
    not_null: bool = False
    unique: bool = False
    accepted_values: list[Any] = field(default_factory=list)


@dataclass
class DbtRelation:
    unique_id: str
    resource_type: str
    name: str
    relation: str
    database: str
    schema: str
    alias: str
    materialized: str = ""
    description: str = ""
    columns: dict[str, DbtColumn] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    primary_key_source: str = ""
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DbtProject:
    project_name: str
    adapter_type: str
    dbt_version: str
    relations: dict[str, DbtRelation]

    def find(self, name: str) -> DbtRelation:
        """A relation by unique_id, model/seed/snapshot name, or relation string."""
        if name in self.relations:
            return self.relations[name]
        matches = [
            row
            for row in self.relations.values()
            if name in (row.name, row.relation) and row.resource_type != "source"
        ] or [row for row in self.relations.values() if name == row.relation]
        if len(matches) != 1:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"dbt model {name!r} " + ("is ambiguous" if matches else "is not in the manifest"),
                details={"model": name, "candidates": [row.unique_id for row in matches]},
            )
        return matches[0]


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"dbt {label} not found at '{path}'; run `dbt build` (and `dbt docs generate` for "
            "catalog.json) first",
            details={"path": str(path)},
        ) from exc
    except (OSError, ValueError) as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"dbt {label} at '{path}' is not readable JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise SemanticLayerError("INVALID_CONFIG", f"dbt {label} at '{path}' is not an object")
    return payload


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticLayerError("INVALID_CONFIG", f"dbt {label} must be an object")
    return value


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(name) for name in value] if isinstance(value, list) else []


def load_dbt_artifacts(
    target_dir: str | os.PathLike[str] | None = None,
    *,
    manifest_path: str | os.PathLike[str] | None = None,
    catalog_path: str | os.PathLike[str] | None = None,
) -> DbtProject:
    """Load ``manifest.json`` (required) and ``catalog.json`` (optional: column types).

    ``target_dir`` is dbt's ``target/`` directory; explicit paths override it.
    """
    target = Path(target_dir) if target_dir is not None else None
    manifest_file = (
        Path(manifest_path) if manifest_path else (target or Path(".")) / "manifest.json"
    )
    manifest = _read_json(manifest_file, "manifest.json")
    catalog_file = (
        Path(catalog_path) if catalog_path else (target / "catalog.json" if target else None)
    )
    catalog = (
        _read_json(catalog_file, "catalog.json")
        if catalog_file is not None and (catalog_path or catalog_file.exists())
        else {}
    )
    metadata = _mapping(manifest.get("metadata", {}), label="manifest metadata")
    nodes = _mapping(manifest.get("nodes", {}), label="manifest nodes")
    sources = _mapping(manifest.get("sources", {}), label="manifest sources")
    entries = {
        key: dict(value)
        for key, value in {**nodes, **sources}.items()
        if isinstance(value, dict) and value.get("resource_type") in _RELATION_RESOURCES
    }
    databases = Counter(str(entry.get("database") or "") for entry in entries.values())
    default_database = databases.most_common(1)[0][0] if databases else ""
    catalog_entries = {
        **_mapping(catalog.get("nodes", {}), label="catalog nodes"),
        **_mapping(catalog.get("sources", {}), label="catalog sources"),
    }
    relations = {
        key: _relation(key, entry, catalog_entries.get(key, {}), default_database)
        for key, entry in entries.items()
    }
    _apply_tests(relations, nodes)
    _resolve_contract_foreign_keys(relations)
    return DbtProject(
        project_name=str(metadata.get("project_name") or ""),
        adapter_type=str(metadata.get("adapter_type") or ""),
        dbt_version=str(metadata.get("dbt_version") or ""),
        relations=relations,
    )


def _relation(
    unique_id: str, entry: dict[str, Any], catalog: dict[str, Any], default_database: str
) -> DbtRelation:
    resource_type = str(entry.get("resource_type"))
    database = str(entry.get("database") or "")
    schema = str(entry.get("schema") or "")
    alias = str(entry.get("alias") or entry.get("identifier") or entry.get("name") or "")
    parts = [part for part in (schema, alias) if part]
    if database and database != default_database:
        parts.insert(0, database)
    columns: dict[str, DbtColumn] = {}
    catalog_columns = {
        str(name).lower(): dict(spec or {})
        for name, spec in dict(catalog.get("columns", {}) or {}).items()
    }
    ordered = sorted(catalog_columns.values(), key=lambda spec: int(spec.get("index") or 0))
    for spec in ordered:
        name = str(spec.get("name") or "")
        if name:
            columns[name.lower()] = DbtColumn(name=name, data_type=str(spec.get("type") or ""))
    primary_key: list[str] = []
    foreign_keys: list[dict[str, Any]] = []
    for name, spec in dict(entry.get("columns", {}) or {}).items():
        spec = dict(spec or {})
        column = columns.setdefault(
            str(name).lower(), DbtColumn(name=str(spec.get("name") or name))
        )
        column.description = str(spec.get("description") or column.description)
        column.data_type = column.data_type or str(spec.get("data_type") or "")
        for constraint in list(spec.get("constraints", []) or []):
            constraint = dict(constraint)
            kind = str(constraint.get("type") or "")
            if kind == "not_null":
                column.not_null = True
            elif kind == "unique":
                column.unique = True
            elif kind == "primary_key":
                primary_key = [column.name]
            elif kind == "foreign_key":
                foreign_keys.append(
                    {
                        "columns": [column.name],
                        "to": str(constraint.get("to") or constraint.get("expression") or ""),
                        "to_columns": _names(constraint.get("to_columns")),
                        "source": "contract",
                    }
                )
    for constraint in list(entry.get("constraints", []) or []):
        constraint = dict(constraint)
        if constraint.get("type") == "primary_key" and constraint.get("columns"):
            primary_key = [str(name) for name in constraint["columns"]]
        if constraint.get("type") == "foreign_key" and _names(constraint.get("columns")):
            foreign_keys.append(
                {
                    "columns": _names(constraint["columns"]),
                    "to": str(constraint.get("to") or constraint.get("expression") or ""),
                    "to_columns": _names(constraint.get("to_columns")),
                    "source": "contract",
                }
            )
    return DbtRelation(
        unique_id=unique_id,
        resource_type=resource_type,
        name=str(entry.get("name") or alias),
        relation=".".join(parts),
        database=database,
        schema=schema,
        alias=alias,
        materialized=str(dict(entry.get("config", {}) or {}).get("materialized") or ""),
        description=str(entry.get("description") or ""),
        columns=columns,
        primary_key=primary_key,
        primary_key_source="contract" if primary_key else "",
        foreign_keys=foreign_keys,
    )


def _target_of(to: str, relations: dict[str, DbtRelation]) -> DbtRelation | None:
    ref = _REF.search(to)
    if ref:
        package, name = (ref.group(1), ref.group(2)) if ref.group(2) else ("", ref.group(1))
        found = [
            row
            for row in relations.values()
            if row.name == name
            and row.resource_type != "source"
            and (not package or row.unique_id.startswith(f"{row.resource_type}.{package}."))
        ]
        return found[0] if len(found) == 1 else None
    source = _SOURCE.search(to)
    if source:
        found = [
            row
            for row in relations.values()
            if row.resource_type == "source"
            and row.unique_id.endswith(f".{source.group(1)}.{source.group(2)}")
        ]
        return found[0] if len(found) == 1 else None
    found = [
        row
        for row in relations.values()
        if to
        in (
            row.unique_id,
            row.relation,
            row.alias,
            f"{row.database}.{row.relation}" if row.database else "",
        )
    ]
    return found[0] if len(found) == 1 else None


def _resolve_contract_foreign_keys(relations: dict[str, DbtRelation]) -> None:
    for relation in relations.values():
        for foreign_key in relation.foreign_keys:
            if foreign_key["source"] != "contract":
                continue
            target = _target_of(str(foreign_key["to"]), relations)
            if target is not None:
                foreign_key["to"] = target.unique_id
                foreign_key["to_relation"] = target.relation


def _apply_tests(relations: dict[str, DbtRelation], nodes: dict[str, Any]) -> None:
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        metadata = dict(node.get("test_metadata", {}) or {})
        kwargs = dict(metadata.get("kwargs", {}) or {})
        attached = str(node.get("attached_node") or "")
        if not attached:
            depends = list(dict(node.get("depends_on", {}) or {}).get("nodes", []) or [])
            attached = str(depends[-1]) if depends else ""
        relation = relations.get(attached)
        if relation is None:
            continue
        column_name = str(node.get("column_name") or kwargs.get("column_name") or "")
        # A column test proves the column exists, even when neither the catalog
        # nor the model's YAML lists it.
        column = (
            relation.columns.setdefault(column_name.lower(), DbtColumn(name=column_name))
            if column_name
            else None
        )
        test = str(metadata.get("name") or "")
        if test == "not_null" and column is not None:
            column.not_null = True
        elif test == "unique" and column is not None:
            column.unique = True
        elif test == "accepted_values" and column is not None:
            column.accepted_values = list(kwargs.get("values", []) or [])
        elif test == "unique_combination_of_columns" and not relation.primary_key:
            combination = [str(name) for name in kwargs.get("combination_of_columns", []) or []]
            if combination:
                relation.primary_key = combination
                relation.primary_key_source = "unique_combination_of_columns test"
        elif test == "relationships" and column is not None:
            target = _target_of(str(kwargs.get("to") or ""), relations)
            if target is not None:
                relation.foreign_keys.append(
                    {
                        "columns": [column.name],
                        "to": target.unique_id,
                        "to_relation": target.relation,
                        "to_columns": [str(kwargs.get("field") or column.name)],
                        "source": "relationships test",
                    }
                )
    for relation in relations.values():
        if relation.primary_key:
            continue
        keys = [
            column.name for column in relation.columns.values() if column.unique and column.not_null
        ]
        if keys:
            relation.primary_key = [keys[0]]
            relation.primary_key_source = "unique and not_null tests"


def suggest_models_from_dbt(
    project: DbtProject, select: list[str] | None = None
) -> list[dict[str, Any]]:
    """One suggestion per dbt model (``select`` narrows by name or unique_id).

    Keys, foreign keys and value sets come from dbt tests and contracts (high
    confidence); times, dimensions and measures from column types and names.
    Sources and seeds are left out unless selected explicitly.
    """
    chosen = (
        [project.find(name) for name in select]
        if select
        else [row for row in project.relations.values() if row.resource_type == "model"]
    )
    return [_suggest(project, relation) for relation in chosen]


def _suggest(project: DbtProject, relation: DbtRelation) -> dict[str, Any]:
    entity = entity_name(relation.alias or relation.name)
    key_columns = list(relation.primary_key)
    key = (
        {
            "columns": key_columns,
            "confidence": "high",
            "reason": f"dbt {relation.primary_key_source}",
        }
        if key_columns
        else None
    )
    links = [
        {
            "columns": list(fk["columns"]),
            **({"column": fk["columns"][0]} if len(fk["columns"]) == 1 else {}),
            "references": {
                "relation": fk.get("to_relation") or fk["to"],
                "columns": list(fk.get("to_columns") or []),
                "dbt_unique_id": fk["to"],
            },
            "confidence": "high",
            "reason": f"dbt {fk['source']}",
        }
        for fk in relation.foreign_keys
        if fk["columns"]
    ]
    linked = {column for link in links for column in link["columns"]}
    times: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = (
        [
            {
                "key": f"{entity}_count",
                "kind": "entity_count",
                "aggregation": "count_distinct",
                "confidence": "high",
                "reason": f"counts {entity} rows by their key",
            }
        ]
        if len(key_columns) == 1
        else []
    )
    untyped: list[str] = []
    for column in relation.columns.values():
        if column.name in key_columns or column.name in linked:
            continue
        role = classify_column(column.name, column.data_type)
        if role == "unknown":
            untyped.append(column.name)
            continue
        described = {"description": column.description} if column.description else {}
        if role == "time":
            times.append(
                {
                    "column": column.name,
                    "kind": "date" if column.data_type.upper().startswith("DATE") else "timestamp",
                    "confidence": "high" if column.not_null else "medium",
                    "reason": f"{column.data_type or 'time'} column"
                    + (" with a not_null test" if column.not_null else ""),
                    **described,
                }
            )
        elif role == "measure":
            aggregation, confidence, reason = measure_aggregation(column.name)
            measures.append(
                {
                    "key": column.name,
                    "kind": "aggregate",
                    "aggregation": aggregation,
                    "confidence": confidence,
                    "reason": reason,
                    **described,
                }
            )
        elif role == "dimension":
            dimensions.append(
                {
                    "column": column.name,
                    "confidence": "high" if column.accepted_values else "medium",
                    "reason": (
                        f"accepted_values test: {len(column.accepted_values)} values"
                        if column.accepted_values
                        else f"{column.data_type or 'text'} column"
                    ),
                    **({"values": column.accepted_values} if column.accepted_values else {}),
                    **described,
                }
            )
    return {
        "relation": relation.relation,
        "dbt_unique_id": relation.unique_id,
        "materialized": relation.materialized,
        "entity": entity,
        "description": relation.description,
        "primary_key": key,
        "times": times,
        "dimensions": dimensions,
        "measures": measures,
        "foreign_keys": links,
        "untyped_columns": untyped,
        "upsert_model": upsert_model_draft(
            entity=entity,
            relation=relation.relation,
            key_columns=key_columns,
            times=times,
            dimensions=dimensions,
            measures=measures,
            description=relation.description,
        ),
    }


def dbt_import_models(
    project: DbtProject, select: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``ArchitectProject.upsert_models`` items for the selected dbt models, and those left out.

    Foreign keys retain the selected target's manifest identity where known;
    otherwise they resolve by relation only when the package has one eligible
    semantic target. A model without a key in dbt
    (no contract primary key, uniqueness tests or column-combination test) is
    left out, with the reason.
    """
    if not select:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            "select the dbt models to import (suggest_models_from_dbt lists them)",
        )
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for suggestion in suggest_models_from_dbt(project, select):
        draft = dict(suggestion["upsert_model"])
        if not draft["primary_key"]:
            skipped.append(
                {
                    "dbt_model": suggestion["dbt_unique_id"],
                    "relation": suggestion["relation"],
                    "reason": "no key in dbt: add unique and not_null tests or an enforced "
                    "contract primary_key, or model it with suggest_model",
                }
            )
            continue
        draft["dbt_unique_id"] = suggestion["dbt_unique_id"]
        draft["references"] = [
            {
                "relation": link["references"]["relation"],
                "columns": list(link["columns"]),
                "to_columns": list(link["references"].get("columns") or []),
                **(
                    {"target_dbt_unique_id": link["references"]["dbt_unique_id"]}
                    if link["references"].get("dbt_unique_id") in project.relations
                    else {}
                ),
            }
            for link in suggestion["foreign_keys"]
        ]
        items.append(draft)
    return items, skipped
