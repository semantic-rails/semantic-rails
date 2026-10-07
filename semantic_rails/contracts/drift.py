"""Static drift against the producer's semantic validation-contract shape."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import LoadedPackageSnapshot

from .producer import CONTRACT_FORMAT_VERSION, export_semantic_contract


def _invalid(reason: str) -> NoReturn:
    raise SemanticLayerError(
        "INVALID_CONTRACT",
        "The semantic contract could not be compared.",
        details={"reason": reason},
    )


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _index(committed: Any) -> dict[str, dict[str, Mapping[str, Any]]]:
    # Validate every comparison row before indexing; duplicates must never be
    # silently overwritten, including in packages other than the selected one.
    if not isinstance(committed, Mapping):
        _invalid("not_mapping")
    if "semantic_rails_contracts" in committed:
        committed = committed["semantic_rails_contracts"]
        if not isinstance(committed, Mapping):
            _invalid("not_mapping")
    version = committed.get("contract_format_version")
    if type(version) is not int or version != CONTRACT_FORMAT_VERSION:
        _invalid("unsupported_version")
    semantic = committed.get("semantic")
    if not isinstance(semantic, Mapping):
        _invalid("invalid_semantic")
    packages = semantic.get("packages")
    if not isinstance(packages, list):
        _invalid("invalid_packages")
    index: dict[str, dict[str, Mapping[str, Any]]] = {}
    for package in packages:
        if not isinstance(package, Mapping) or not _text(package.get("package_id")):
            _invalid("invalid_package")
        package_id = package["package_id"]
        if package_id in index:
            _invalid("duplicate_package")
        resources = package.get("resources")
        if not isinstance(resources, list):
            _invalid("invalid_resources")
        models: dict[str, Mapping[str, Any]] = {}
        for model in resources:
            if (
                not isinstance(model, Mapping)
                or not _text(model.get("semantic_model_id"))
                or ("relation" in model and not isinstance(model["relation"], str))
            ):
                _invalid("invalid_model")
            model_id = model["semantic_model_id"]
            if model_id in models:
                _invalid("duplicate_model")
            columns = model.get("columns")
            if not isinstance(columns, list):
                _invalid("invalid_columns")
            names: set[str] = set()
            for column in columns:
                if (
                    not isinstance(column, Mapping)
                    or not _text(column.get("name"))
                    or ("data_type" in column and not _text(column["data_type"]))
                    or not isinstance(column.get("required_by"), list)
                    or not all(_text(value) for value in column["required_by"])
                    or len(set(column["required_by"])) != len(column["required_by"])
                ):
                    _invalid("invalid_column")
                name = column["name"].casefold()
                if name in names:
                    _invalid("duplicate_column")
                names.add(name)
            models[model_id] = model
        index[package_id] = models
    return index


def _family(data_type: str) -> str | None:
    value = re.sub(r"\([^)]*\)", "", data_type).strip().lower()
    value = " ".join(value.split())
    if value in {"string", "varchar", "text", "character varying", "char", "uuid"}:
        return "text"
    if value in {
        "integer",
        "int",
        "bigint",
        "smallint",
        "hugeint",
        "number",
        "numeric",
        "decimal",
        "double",
        "float",
        "real",
    }:
        return "number"
    if value in {"boolean", "bool"}:
        return "boolean"
    if value in {"date", "datetime", "timestamptz"} or value.startswith("timestamp"):
        return "temporal"
    return None


def diff_semantic_contract(
    package: str | Path | LoadedPackageSnapshot, committed: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare a package with parsed composed v1 data, without warehouse access.

    The optional ``semantic_rails_contracts`` wrapper is accepted once. Hosts
    own file reading and YAML parsing; ``binding`` and fingerprint/version
    metadata are ignored. Only covered models can produce drift.
    """
    index = _index(committed)
    exported = export_semantic_contract(package, physical_types=False)
    current = exported["semantic"]["packages"][0]
    package_id = current["package_id"]
    if package_id not in index:
        _invalid("package_not_found")
    previous = index[package_id]
    models = _index(exported)[package_id]
    drift: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []

    def add(
        target: list[dict[str, Any]],
        code: str,
        model_id: str,
        model: Mapping[str, Any],
        message: str,
        column: Mapping[str, Any] | None = None,
        contract_column: str | None = None,
    ) -> None:
        target.append(
            {
                "code": code,
                "semantic_model_id": model_id,
                "relation": model.get("relation"),
                "column": column["name"] if column else None,
                "required_by": list(column["required_by"]) if column else [],
                "contract_column": contract_column,
                "message": message,
            }
        )

    for model_id, model in sorted(models.items()):
        if model_id not in previous:
            add(
                notes,
                "CONTRACT_MODEL_NOT_COVERED",
                model_id,
                model,
                "Model is not in the contract.",
            )
            continue
        old = previous[model_id]
        # The runtime's raw relation form splits on dots and quotes each part.
        if old.get("relation") and [part.casefold() for part in old["relation"].split(".")] != [
            part.casefold() for part in model.get("relation", "").split(".")
        ]:
            add(
                drift,
                "RELATION_CHANGED",
                model_id,
                model,
                f"Relation changed from {old['relation']} to {model.get('relation')}.",
            )
        old_columns = {row["name"].casefold(): row for row in old["columns"]}
        columns = {row["name"].casefold(): row for row in model["columns"]}
        for name, column in sorted(columns.items()):
            prior = old_columns.get(name)
            if prior is None:
                before = sorted(
                    row["name"]
                    for row in old["columns"]
                    if set(row["required_by"]) & set(column["required_by"])
                )
                message = f"Column {column['name']} is missing from the contract."
                if before:
                    message += f" Its semantic objects previously read {', '.join(before)}."
                add(
                    drift,
                    "CONTRACT_COLUMN_MISSING",
                    model_id,
                    model,
                    message,
                    column,
                    before[0] if len(before) == 1 else None,
                )
                continue
            hint, physical = (
                _family(column.get("data_type", "")),
                _family(prior.get("data_type", "")),
            )
            if hint is None or physical is None:
                add(
                    notes,
                    "TYPE_NOT_COMPARED",
                    model_id,
                    model,
                    f"Types not compared for {column['name']}: missing or unknown type.",
                    column,
                    prior["name"],
                )
            elif hint != physical:
                add(
                    drift,
                    "COLUMN_TYPE_CHANGED",
                    model_id,
                    model,
                    f"Type family changed for {column['name']}: {prior['data_type']} to {column['data_type']}.",
                    column,
                    prior["name"],
                )
        for name in sorted(old_columns.keys() - columns.keys()):
            column = old_columns[name]
            add(
                notes,
                "COLUMN_UNUSED",
                model_id,
                model,
                f"Column {column['name']} is no longer read.",
                column,
                column["name"],
            )
    for model_id in sorted(previous.keys() - models.keys()):
        model = previous[model_id]
        add(notes, "COLUMN_UNUSED", model_id, model, "Model is no longer read.")
    return {
        "ok": not drift,
        "package_id": package_id,
        "covered_models": sorted(models.keys() & previous.keys()),
        "drift": drift,
        "notes": notes,
    }
