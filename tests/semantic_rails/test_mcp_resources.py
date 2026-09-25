"""Legacy resource bodies remain readable; smaller projections are opt-in."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


def test_legacy_capabilities_retains_full_tool_definitions(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    payload = adapter.read_resource("semantic-rails://capabilities")["payload"]
    assert payload["interface_version"] == "v2"
    assert payload["tools"] == list_tool_definitions()
    first = payload["tools"][0]
    assert first["description"]
    assert first["inputSchema"]["properties"]["request_id"]["type"] == "string"
    assert first["outputSchema"]["type"] == "object"
    assert first["annotations"]["title"]
    assert [row["name"] for row in payload["prompts"]]


def test_opt_in_capabilities_summary_indexes_tools_without_copying_schemas(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    compact = json.loads(adapter.read_resource("semantic-rails://capabilities/summary")["text"])
    legacy = adapter.read_resource("semantic-rails://capabilities")["payload"]
    assert compact["tools"] == [
        {"name": tool["name"], "title": tool["annotations"]["title"]} for tool in legacy["tools"]
    ]
    assert len(json.dumps(compact)) * 5 < len(json.dumps(legacy))
    expected_uris = {
        "semantic-rails://capabilities",
        "semantic-rails://capabilities/summary",
        "semantic-rails://catalog/summary",
        "semantic-rails://catalog/index",
        "semantic-rails://catalog/full",
    }
    assert {row["uri"] for row in compact["resources"]} == expected_uris
    assert {row["uri"] for row in legacy["resources"]} == expected_uris


def test_legacy_catalog_summary_retains_rows_and_counts(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    catalog = adapter.read_resource("semantic-rails://catalog/summary")["payload"]["catalog"]
    assert catalog["measures"][0]["id"]
    assert catalog["counts_total"]["measures"] >= len(catalog["measures"])
    assert catalog["meta"]["verbosity"] == "compact"


def test_opt_in_catalog_index_is_small_and_full_is_whole(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    index = adapter.read_resource("semantic-rails://catalog/index")
    catalog = index["payload"]["catalog"]
    assert catalog == adapter.call_tool("discover", {"terms": ""})["catalog"]
    assert catalog["meta"]["verbosity"] == "summary"
    assert catalog["measure_ids"]
    legacy = adapter.read_resource("semantic-rails://catalog/summary")
    assert len(index["text"]) * 10 < len(legacy["text"])
    full = adapter.read_resource("semantic-rails://catalog/full")
    assert full["payload"]["catalog"]["meta"]["verbosity"] == "full"
    assert "alias_index" in full["payload"]["catalog"]
