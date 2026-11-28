"""The Architect MCP contract's major bump from v1 to v2.

v2 dropped the ``upgrade_project`` tool. A removal needs a new contract major, so the
compatibility checker accepts the declared bump, and asking for the retired v1 manifest
names v2 instead of failing as an unknown contract.
"""

from __future__ import annotations

import json
import runpy
from copy import deepcopy
from pathlib import Path

import pytest

from semantic_rails.contracts import contract_path, load_contract
from semantic_rails.contracts.compatibility import (
    compare_contract_bundles,
    load_contract_directory,
)
from semantic_rails.contracts.generation import generated_artifacts
from semantic_rails.errors import SemanticLayerError

REPO_ROOT = Path(__file__).resolve().parents[2]


# The released fragment's own wording, so the tests read the bump the way a release does.
BREAKING_NOTE = (
    "- **Breaking:** the Architect MCP contract moves from interface v1 to v2 because\n"
    "  `upgrade_project` was removed. A client that reads `architect_mcp.v1.json` must read\n"
    "  `architect_mcp.v2.json` instead.\n"
)
DECLARED = {"architect_interface_version": "v2", "release_notes": BREAKING_NOTE}


def _architect_v1(manifest: dict) -> dict:
    """The released Architect v1 manifest: stamped v1 and still listing upgrade_project."""
    v1 = deepcopy(manifest)
    v1["$id"] = v1["$id"].replace("architect_mcp.v2.json", "architect_mcp.v1.json")
    v1["interface_version"] = "v1"
    v1["tools"].append({"name": "upgrade_project", "inputSchema": {}, "outputSchema": {}})
    return v1


def _v1_release() -> dict:
    current = generated_artifacts()
    baseline = {**current, "architect_mcp.v1.json": _architect_v1(current["architect_mcp.v2.json"])}
    del baseline["architect_mcp.v2.json"]
    return baseline


def _kinds(rows: list[dict]) -> list[tuple[str, str, str]]:
    return [(row["artifact"], row["path"], row["kind"]) for row in rows]


def test_compatibility_checker_accepts_the_declared_architect_major_bump() -> None:
    report = compare_contract_bundles(_v1_release(), generated_artifacts(), **DECLARED)

    assert report["ok"] is True
    assert report["breaking_changes"] == []
    assert _kinds(report["major_changes"]) == [
        ("architect_mcp.v1.json", "/", "major_bumped"),
        ("architect_mcp.v2.json", "/tools/upgrade_project", "tool_removed"),
    ]
    assert "architect_mcp.v2.json" in report["major_changes"][0]["message"]
    assert ("architect_mcp.v2.json", "/", "artifact_added") in _kinds(report["additive_changes"])
    assert report["summary"] == {"breaking": 0, "additive": 1, "major": 2}


def _architect_v1_removed(report: dict) -> None:
    assert report["ok"] is False
    assert ("architect_mcp.v1.json", "/", "artifact_removed") in _kinds(report["breaking_changes"])
    assert report["major_changes"] == []


@pytest.mark.parametrize(
    ("field", "stamp"),
    [
        ("interface_version", "v1"),
        ("$id", "https://semantic-rails.com/schemas/architect_mcp.v1.json"),
    ],
)
def test_a_v1_manifest_renamed_to_v2_is_not_a_declared_bump(field: str, stamp: str) -> None:
    # The file is called v2, but its payload still says v1.
    current = generated_artifacts()
    current["architect_mcp.v2.json"] = {**current["architect_mcp.v2.json"], field: stamp}

    report = compare_contract_bundles(_v1_release(), current, **DECLARED)

    _architect_v1_removed(report)
    assert field in report["breaking_changes"][0]["message"]


def test_a_jump_from_architect_v1_to_v3_is_not_a_declared_bump() -> None:
    current = generated_artifacts()
    v3 = deepcopy(current.pop("architect_mcp.v2.json"))
    v3["$id"] = v3["$id"].replace("v2", "v3")
    v3["interface_version"] = "v3"
    current["architect_mcp.v3.json"] = v3

    report = compare_contract_bundles(
        _v1_release(),
        current,
        architect_interface_version="v3",
        release_notes=BREAKING_NOTE.replace("v2", "v3"),
    )

    _architect_v1_removed(report)


def test_a_shipped_query_mcp_v2_does_not_permit_removing_query_mcp_v1() -> None:
    current = generated_artifacts()
    baseline = {**current, "query_mcp.v1.json": {"tools": [{"name": "query"}]}}

    report = compare_contract_bundles(baseline, current, **DECLARED)

    assert report["ok"] is False
    assert _kinds(report["breaking_changes"]) == [("query_mcp.v1.json", "/", "artifact_removed")]
    assert report["major_changes"] == []


@pytest.mark.parametrize(
    "declaration",
    [
        # The producer still builds v1.
        {"architect_interface_version": "v1", "release_notes": BREAKING_NOTE},
        # No changelog entry, or one not marked breaking.
        {"architect_interface_version": "v2"},
        {
            "architect_interface_version": "v2",
            "release_notes": BREAKING_NOTE.replace("**Breaking:** t", "T"),
        },
        # A breaking entry that names neither contract file.
        {
            "architect_interface_version": "v2",
            "release_notes": "- **Breaking:** `upgrade_project` was removed.\n",
        },
    ],
)
def test_an_undeclared_architect_major_bump_is_rejected(declaration: dict) -> None:
    _architect_v1_removed(
        compare_contract_bundles(_v1_release(), generated_artifacts(), **declaration)
    )


def test_a_removed_tool_the_breaking_entry_does_not_name_still_breaks() -> None:
    baseline = _v1_release()
    baseline["architect_mcp.v1.json"]["tools"].append({"name": "retired_tool"})

    report = compare_contract_bundles(baseline, generated_artifacts(), **DECLARED)

    assert report["ok"] is False
    assert _kinds(report["breaking_changes"]) == [
        ("architect_mcp.v2.json", "/tools/retired_tool", "tool_removed")
    ]


def test_the_declared_bump_still_compares_the_tools_that_stay() -> None:
    baseline = _v1_release()
    kept = baseline["architect_mcp.v1.json"]["tools"][0]
    kept["inputSchema"] = {**kept["inputSchema"], "properties": {"retired_argument": {}}}

    report = compare_contract_bundles(baseline, generated_artifacts(), **DECLARED)

    assert report["ok"] is False
    assert ("property_removed") in {row["kind"] for row in report["breaking_changes"]}


@pytest.mark.parametrize(
    ("artifact", "removed"),
    [("architect_mcp.v1.json", "upgrade_project"), ("query_mcp.v1.json", "query")],
)
def test_a_patch_release_inside_a_retired_major_still_compares_tools(
    artifact: str, removed: str
) -> None:
    # Both bundles ship the same v1 major; dropping a tool is breaking, declared or not.
    baseline = {artifact: {"tools": [{"name": removed}, {"name": "kept"}]}}
    current = {artifact: {"tools": [{"name": "kept"}]}}

    report = compare_contract_bundles(baseline, current, **DECLARED)

    assert report["ok"] is False
    assert _kinds(report["breaking_changes"]) == [(artifact, f"/tools/{removed}", "tool_removed")]


@pytest.mark.parametrize(
    "current_name",
    [
        # Not the declared next major: a name without a major, a lower major, another contract.
        "architect_mcp.v1.openapi.json",
        "architect_mcp.v0.json",
        "query_mcp.v3.json",
    ],
)
def test_compatibility_checker_still_breaks_without_a_new_major(current_name: str) -> None:
    removed = compare_contract_bundles(
        {"architect_mcp.v1.json": {"tools": []}}, {current_name: {"tools": []}}, **DECLARED
    )
    assert removed["ok"] is False
    assert [(row["artifact"], row["kind"]) for row in removed["breaking_changes"]] == [
        ("architect_mcp.v1.json", "artifact_removed")
    ]
    assert removed["major_changes"] == []


def test_an_architect_downgrade_is_rejected() -> None:
    current = generated_artifacts()
    downgraded = {**_v1_release()}

    report = compare_contract_bundles(current, downgraded, **DECLARED)

    assert report["ok"] is False
    assert ("architect_mcp.v2.json", "/", "artifact_removed") in _kinds(report["breaking_changes"])


def test_the_release_script_reads_this_repository_s_declaration(tmp_path: Path) -> None:
    # The shipped changelog fragment and ARCHITECT_INTERFACE_VERSION declare the bump.
    for name, document in _v1_release().items():
        (tmp_path / name).write_text(json.dumps(document), encoding="utf-8")
    script = runpy.run_path(str(REPO_ROOT / "scripts" / "check_contract_compatibility.py"))

    assert script["main"](["--baseline", str(tmp_path)]) == 0


def test_a_baseline_architect_v1_manifest_is_loaded(tmp_path: Path) -> None:
    (tmp_path / "architect_mcp.v1.json").write_text(json.dumps({"tools": []}), encoding="utf-8")
    assert set(load_contract_directory(tmp_path)) == {"architect_mcp.v1.json"}


@pytest.mark.parametrize("read", [load_contract, contract_path])
def test_asking_for_the_architect_v1_contract_names_v2(read) -> None:
    with pytest.raises(SemanticLayerError) as exc:
        read("architect_mcp.v1.json")

    assert exc.value.code == "INVALID_CONTRACT"
    assert exc.value.details == {
        "reason": "unsupported_version",
        "requested": "architect_mcp.v1.json",
        "requested_version": "v1",
        "current": "architect_mcp.v2.json",
        "current_version": "v2",
    }
    assert str(exc.value) == (
        "Contract 'architect_mcp.v1.json' (major v1) is no longer supported; "
        "use 'architect_mcp.v2.json' (major v2)."
    )
    assert load_contract("architect_mcp.v2.json")["interface_version"] == "v2"


def test_an_unknown_contract_name_is_still_a_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown Semantic Rails contract"):
        load_contract("architect_mcp.v9.json")
