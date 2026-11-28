"""The Architect MCP contract's major bump from v1 to v2.

v2 dropped the ``upgrade_project`` tool. A removal needs a new contract major, so the
compatibility checker accepts the declared bump, and asking for the retired v1 manifest
names v2 instead of failing as an unknown contract.
"""

from __future__ import annotations

import json
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


def test_compatibility_checker_accepts_a_declared_major_bump() -> None:
    # The released Architect v1 manifest listed upgrade_project; v2 does not.
    current = generated_artifacts()
    v1 = deepcopy(current["architect_mcp.v2.json"])
    v1["tools"].append({"name": "upgrade_project", "inputSchema": {}, "outputSchema": {}})
    baseline = {**current, "architect_mcp.v1.json": v1}
    del baseline["architect_mcp.v2.json"]

    report = compare_contract_bundles(baseline, current)

    assert report["ok"] is True
    assert report["breaking_changes"] == []
    assert [(row["artifact"], row["kind"]) for row in report["major_changes"]] == [
        ("architect_mcp.v1.json", "major_bumped")
    ]
    assert "architect_mcp.v2.json" in report["major_changes"][0]["message"]
    assert ("architect_mcp.v2.json", "artifact_added") in {
        (row["artifact"], row["kind"]) for row in report["additive_changes"]
    }
    assert report["summary"] == {"breaking": 0, "additive": 1, "major": 1}


@pytest.mark.parametrize(
    "current_name",
    [
        # Not a new major: a name without a major, a lower major, or another contract.
        "architect_mcp.v1.openapi.json",
        "architect_mcp.v0.json",
        "query_mcp.v3.json",
    ],
)
def test_compatibility_checker_still_breaks_without_a_new_major(current_name: str) -> None:
    removed = compare_contract_bundles(
        {"architect_mcp.v1.json": {"tools": []}}, {current_name: {"tools": []}}
    )
    assert removed["ok"] is False
    assert [(row["artifact"], row["kind"]) for row in removed["breaking_changes"]] == [
        ("architect_mcp.v1.json", "artifact_removed")
    ]
    assert removed["major_changes"] == []


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
