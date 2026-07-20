"""Conservative compatibility checks for released contract bundles.

The checker intentionally prefers false positives over silent breakage. A
contract owner can review and explicitly version an intentional breaking
change; concurrent consumers should never discover it only after release.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import CONTRACT_NAMES

_JSON_SCHEMA_ARTIFACTS = {
    "package.v1.json",
    "query_ir.v1.json",
    "semantic_contract.v1.json",
    "validation_report.v1.json",
}


def load_contract_directory(path: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(path)
    return {
        name: dict(json.loads((root / name).read_text(encoding="utf-8")))
        for name in CONTRACT_NAMES
        if (root / name).is_file()
    }


def _change(
    changes: list[dict[str, str]],
    *,
    artifact: str,
    path: str,
    kind: str,
    message: str,
) -> None:
    changes.append(
        {
            "artifact": artifact,
            "path": path,
            "kind": kind,
            "message": message,
        }
    )


def _schema_changes(
    before: Any,
    after: Any,
    *,
    artifact: str,
    path: str,
    breaking: list[dict[str, str]],
    additive: list[dict[str, str]],
) -> None:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return
    before_required = {str(item) for item in list(before.get("required", []) or [])}
    after_required = {str(item) for item in list(after.get("required", []) or [])}
    for name in sorted(after_required - before_required):
        _change(
            breaking,
            artifact=artifact,
            path=path,
            kind="required_added",
            message=f"New required field {name!r}.",
        )

    before_properties = dict(before.get("properties", {}) or {})
    after_properties = dict(after.get("properties", {}) or {})
    for name in sorted(before_properties.keys() - after_properties.keys()):
        _change(
            breaking,
            artifact=artifact,
            path=path,
            kind="property_removed",
            message=f"Property {name!r} was removed.",
        )
    for name in sorted(after_properties.keys() - before_properties.keys()):
        _change(
            additive,
            artifact=artifact,
            path=path,
            kind="property_added",
            message=f"Optional property {name!r} was added.",
        )
    for name in sorted(before_properties.keys() & after_properties.keys()):
        _schema_changes(
            before_properties[name],
            after_properties[name],
            artifact=artifact,
            path=f"{path}/properties/{name}",
            breaking=breaking,
            additive=additive,
        )

    before_defs = dict(before.get("$defs", {}) or {})
    after_defs = dict(after.get("$defs", {}) or {})
    for name in sorted(before_defs.keys() - after_defs.keys()):
        _change(
            breaking,
            artifact=artifact,
            path=path,
            kind="definition_removed",
            message=f"Schema definition {name!r} was removed.",
        )
    for name in sorted(before_defs.keys() & after_defs.keys()):
        _schema_changes(
            before_defs[name],
            after_defs[name],
            artifact=artifact,
            path=f"{path}/$defs/{name}",
            breaking=breaking,
            additive=additive,
        )

    before_enum = set(before.get("enum", []) or [])
    after_enum = set(after.get("enum", []) or [])
    removed_enum = before_enum - after_enum
    if before_enum and removed_enum:
        _change(
            breaking,
            artifact=artifact,
            path=path,
            kind="enum_narrowed",
            message=f"Enum values were removed: {sorted(removed_enum, key=str)!r}.",
        )
    if "const" in before and before.get("const") != after.get("const"):
        _change(
            breaking,
            artifact=artifact,
            path=path,
            kind="const_changed",
            message=f"Constant changed from {before.get('const')!r} to {after.get('const')!r}.",
        )
    if "type" in before and before.get("type") != after.get("type"):
        _change(
            breaking,
            artifact=artifact,
            path=path,
            kind="type_changed",
            message=f"Type changed from {before.get('type')!r} to {after.get('type')!r}.",
        )
    if (
        before.get("additionalProperties", True) is not False
        and after.get("additionalProperties", True) is False
    ):
        _change(
            breaking,
            artifact=artifact,
            path=path,
            kind="additional_properties_closed",
            message="Previously open object now rejects additional properties.",
        )
    if isinstance(before.get("items"), Mapping) and isinstance(after.get("items"), Mapping):
        _schema_changes(
            before["items"],
            after["items"],
            artifact=artifact,
            path=f"{path}/items",
            breaking=breaking,
            additive=additive,
        )


def _http_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    breaking: list[dict[str, str]],
    additive: list[dict[str, str]],
) -> None:
    artifact = "http_api.v1.openapi.json"
    before_paths = dict(before.get("paths", {}) or {})
    after_paths = dict(after.get("paths", {}) or {})
    for path, operations in before_paths.items():
        after_operations = dict(after_paths.get(path, {}) or {})
        for method in dict(operations or {}):
            if method not in after_operations:
                _change(
                    breaking,
                    artifact=artifact,
                    path=f"{path}/{method}",
                    kind="operation_removed",
                    message=f"HTTP operation {method.upper()} {path} was removed.",
                )
    for path, operations in after_paths.items():
        before_operations = dict(before_paths.get(path, {}) or {})
        for method in dict(operations or {}):
            if method not in before_operations:
                _change(
                    additive,
                    artifact=artifact,
                    path=f"{path}/{method}",
                    kind="operation_added",
                    message=f"HTTP operation {method.upper()} {path} was added.",
                )


def _mcp_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    artifact: str,
    breaking: list[dict[str, str]],
    additive: list[dict[str, str]],
) -> None:
    before_tools = {str(row["name"]): row for row in list(before.get("tools", []) or [])}
    after_tools = {str(row["name"]): row for row in list(after.get("tools", []) or [])}
    for name in sorted(before_tools.keys() - after_tools.keys()):
        _change(
            breaking,
            artifact=artifact,
            path=f"/tools/{name}",
            kind="tool_removed",
            message=f"MCP tool {name!r} was removed.",
        )
    for name in sorted(after_tools.keys() - before_tools.keys()):
        _change(
            additive,
            artifact=artifact,
            path=f"/tools/{name}",
            kind="tool_added",
            message=f"MCP tool {name!r} was added.",
        )
    for name in sorted(before_tools.keys() & after_tools.keys()):
        _schema_changes(
            dict(before_tools[name].get("inputSchema", {}) or {}),
            dict(after_tools[name].get("inputSchema", {}) or {}),
            artifact=artifact,
            path=f"/tools/{name}/inputSchema",
            breaking=breaking,
            additive=additive,
        )
        _schema_changes(
            dict(before_tools[name].get("outputSchema", {}) or {}),
            dict(after_tools[name].get("outputSchema", {}) or {}),
            artifact=artifact,
            path=f"/tools/{name}/outputSchema",
            breaking=breaking,
            additive=additive,
        )


def compare_contract_bundles(
    baseline: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare two bundles and classify conservative breaking/additive drift."""

    breaking: list[dict[str, str]] = []
    additive: list[dict[str, str]] = []
    for name in sorted(set(baseline) - set(current)):
        _change(
            breaking,
            artifact=name,
            path="/",
            kind="artifact_removed",
            message=f"Contract artifact {name!r} was removed.",
        )
    for name in sorted(set(current) - set(baseline)):
        _change(
            additive,
            artifact=name,
            path="/",
            kind="artifact_added",
            message=f"Contract artifact {name!r} was added.",
        )
    for name in sorted(_JSON_SCHEMA_ARTIFACTS & baseline.keys() & current.keys()):
        _schema_changes(
            baseline[name],
            current[name],
            artifact=name,
            path="/",
            breaking=breaking,
            additive=additive,
        )
    if "http_api.v1.openapi.json" in baseline and "http_api.v1.openapi.json" in current:
        _http_changes(
            baseline["http_api.v1.openapi.json"],
            current["http_api.v1.openapi.json"],
            breaking=breaking,
            additive=additive,
        )
    for artifact in ("architect_mcp.v1.json", "query_mcp.v1.json"):
        if artifact in baseline and artifact in current:
            _mcp_changes(
                baseline[artifact],
                current[artifact],
                artifact=artifact,
                breaking=breaking,
                additive=additive,
            )
    return {
        "ok": not breaking,
        "breaking_changes": breaking,
        "additive_changes": additive,
        "summary": {
            "breaking": len(breaking),
            "additive": len(additive),
        },
    }
