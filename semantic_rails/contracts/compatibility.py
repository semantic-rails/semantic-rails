"""Conservative compatibility checks for released contract bundles.

The checker intentionally prefers false positives over silent breakage. A
contract owner can review and explicitly version an intentional breaking
change; concurrent consumers should never discover it only after release.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import CONTRACT_NAMES

_JSON_SCHEMA_ARTIFACTS = {
    "package.v1.json",
    "metric_portability.v1.json",
    "query.v2.json",
    "semantic_contract.v1.json",
    "validation_report.v1.json",
}


# Contracts a release stopped shipping; a baseline that has one reports it removed.
_RETIRED_CONTRACT_NAMES = (
    "architect_mcp.v1.json",
    "query_mcp.v1.json",
    "query_ir.preview.v2.json",
    "query_ir.v1.json",
)

# Every MCP manifest major the loader recognizes. Whatever both bundles ship is compared.
_MCP_ARTIFACTS = (
    "architect_mcp.v1.json",
    "architect_mcp.v2.json",
    "query_mcp.v1.json",
    "query_mcp.v2.json",
)

# The one declared major transition: Architect MCP v1 to v2, which removed tools.
_TRANSITION_FROM, _TRANSITION_TO, _TRANSITION_MAJOR = (
    "architect_mcp.v1.json",
    "architect_mcp.v2.json",
    "v2",
)


def _transition_gaps(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    architect_interface_version: str | None,
    notes: list[str],
) -> list[str]:
    """Why the Architect v1 to v2 transition is not declared; empty when it is."""

    if _TRANSITION_TO in baseline or _TRANSITION_TO not in current:
        return [f"the current bundle does not add {_TRANSITION_TO!r}"]
    manifest = current[_TRANSITION_TO]
    gaps: list[str] = []
    if architect_interface_version != _TRANSITION_MAJOR:
        gaps.append(f"ARCHITECT_INTERFACE_VERSION is {architect_interface_version!r}")
    if manifest.get("interface_version") != _TRANSITION_MAJOR:
        gaps.append(f"its interface_version is {manifest.get('interface_version')!r}")
    if not str(manifest.get("$id", "")).endswith(f"/{_TRANSITION_TO}"):
        gaps.append(f"its $id is {manifest.get('$id')!r}")
    if not notes:
        gaps.append("no **Breaking:** changelog entry names both artifacts")
    return gaps


def _breaking_notes(release_notes: str) -> list[str]:
    """The ``- **Breaking:**`` changelog entries that name both transition artifacts."""

    return [
        entry
        for entry in re.split(r"(?m)^- ", release_notes)
        if entry.startswith("**Breaking:**")
        and f"`{_TRANSITION_FROM}`" in entry
        and f"`{_TRANSITION_TO}`" in entry
    ]


def load_contract_directory(path: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(path)
    return {
        name: dict(json.loads((root / name).read_text(encoding="utf-8")))
        for name in (*CONTRACT_NAMES, *_RETIRED_CONTRACT_NAMES)
        if (root / name).is_file()
    }


def _change(
    changes: list[dict[str, str]], artifact: str, path: str, kind: str, message: str
) -> None:
    changes.append({"artifact": artifact, "path": path, "kind": kind, "message": message})


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
        _change(breaking, artifact, path, "required_added", f"New required field {name!r}.")

    before_properties = dict(before.get("properties", {}) or {})
    after_properties = dict(after.get("properties", {}) or {})
    for name in sorted(before_properties.keys() - after_properties.keys()):
        _change(breaking, artifact, path, "property_removed", f"Property {name!r} was removed.")
    for name in sorted(after_properties.keys() - before_properties.keys()):
        _change(
            additive, artifact, path, "property_added", f"Optional property {name!r} was added."
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
            artifact,
            path,
            "definition_removed",
            f"Schema definition {name!r} was removed.",
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
            artifact,
            path,
            "enum_narrowed",
            f"Enum values were removed: {sorted(removed_enum, key=str)!r}.",
        )
    if "const" in before and before.get("const") != after.get("const"):
        _change(
            breaking,
            artifact,
            path,
            "const_changed",
            f"Constant changed from {before.get('const')!r} to {after.get('const')!r}.",
        )
    if "type" in before and before.get("type") != after.get("type"):
        _change(
            breaking,
            artifact,
            path,
            "type_changed",
            f"Type changed from {before.get('type')!r} to {after.get('type')!r}.",
        )
    if (
        before.get("additionalProperties", True) is not False
        and after.get("additionalProperties", True) is False
    ):
        _change(
            breaking,
            artifact,
            path,
            "additional_properties_closed",
            "Previously open object now rejects additional properties.",
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
                    artifact,
                    f"{path}/{method}",
                    "operation_removed",
                    f"HTTP operation {method.upper()} {path} was removed.",
                )
    for path, operations in after_paths.items():
        before_operations = dict(before_paths.get(path, {}) or {})
        for method in dict(operations or {}):
            if method not in before_operations:
                _change(
                    additive,
                    artifact,
                    f"{path}/{method}",
                    "operation_added",
                    f"HTTP operation {method.upper()} {path} was added.",
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
            breaking, artifact, f"/tools/{name}", "tool_removed", f"MCP tool {name!r} was removed."
        )
    for name in sorted(after_tools.keys() - before_tools.keys()):
        _change(additive, artifact, f"/tools/{name}", "tool_added", f"MCP tool {name!r} was added.")
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
    *,
    architect_interface_version: str | None = None,
    release_notes: str = "",
) -> dict[str, Any]:
    """Compare two bundles and classify conservative breaking/additive drift.

    Removing a contract is breaking, with one exception: the declared Architect MCP v1 to
    v2 transition. It needs ``architect_interface_version`` (the producer constant) and the
    v2 manifest stamped v2, plus a ``**Breaking:**`` entry in ``release_notes`` naming both
    artifacts. Its removed tools then move to ``major_changes`` when that entry names them.
    """

    breaking: list[dict[str, str]] = []
    additive: list[dict[str, str]] = []
    majors: list[dict[str, str]] = []
    notes = _breaking_notes(release_notes)
    gaps = _transition_gaps(
        baseline, current, architect_interface_version=architect_interface_version, notes=notes
    )
    declared = _TRANSITION_FROM in baseline.keys() - current.keys() and not gaps
    for name in sorted(set(baseline) - set(current)):
        if name == _TRANSITION_FROM and declared:
            message = f"Contract artifact {name!r} was replaced by the declared {_TRANSITION_TO!r}."
            _change(majors, name, "/", "major_bumped", message)
            continue
        message = f"Contract artifact {name!r} was removed."
        if name == _TRANSITION_FROM:
            message += f" {_TRANSITION_TO!r} is not a declared major: {'; '.join(gaps)}."
        _change(breaking, name, "/", "artifact_removed", message)
    for name in sorted(set(current) - set(baseline)):
        _change(additive, name, "/", "artifact_added", f"Contract artifact {name!r} was added.")
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
    for artifact in _MCP_ARTIFACTS:
        if artifact in baseline and artifact in current:
            _mcp_changes(
                baseline[artifact],
                current[artifact],
                artifact=artifact,
                breaking=breaking,
                additive=additive,
            )
    if declared:
        # The v1 tools against v2: a removal the breaking entry names is the declared change.
        transition: list[dict[str, str]] = []
        _mcp_changes(
            baseline[_TRANSITION_FROM],
            current[_TRANSITION_TO],
            artifact=_TRANSITION_TO,
            breaking=transition,
            additive=additive,
        )
        for row in transition:
            tool = row["path"].removeprefix("/tools/")
            named = row["kind"] == "tool_removed" and any(f"`{tool}`" in n for n in notes)
            (majors if named else breaking).append(row)
    return {
        "ok": not breaking,
        "breaking_changes": breaking,
        "additive_changes": additive,
        "major_changes": majors,
        "summary": {
            "breaking": len(breaking),
            "additive": len(additive),
            "major": len(majors),
        },
    }
