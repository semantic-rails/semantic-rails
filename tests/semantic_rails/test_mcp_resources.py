"""MCP resources are what their names say.

``catalog/summary`` returned the catalog at ``compact`` verbosity (tens of
thousands of tokens), and the ``capabilities`` resource embedded a second copy
of every tool definition. The summary resource now matches the catalog tool's
default summary, and the capabilities resource indexes tools by name and title.
"""

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


def _catalog_ids(catalog: dict[str, Any]) -> dict[str, list[Any]]:
    return {key: value for key, value in catalog.items() if key not in {"meta"}}


def test_catalog_summary_resource_matches_the_catalog_tool(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    resource = adapter.read_resource("semantic-rails://catalog/summary")["payload"]["catalog"]
    tool = adapter.call_tool("catalog", {})["catalog"]
    assert resource["meta"]["verbosity"] == tool["meta"]["verbosity"] == "summary"
    assert _catalog_ids(resource) == _catalog_ids(tool)


def test_catalog_summary_is_small_and_full_is_whole(adapter: SemanticLayerMCPAdapter) -> None:
    summary = adapter.read_resource("semantic-rails://catalog/summary")["text"]
    full = adapter.read_resource("semantic-rails://catalog/full")
    assert full["payload"]["catalog"]["meta"]["verbosity"] == "full"
    assert "alias_index" in full["payload"]["catalog"]
    assert len(summary) * 20 < len(full["text"])


def test_capabilities_resource_indexes_tools_without_copying_them(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    resource = json.loads(adapter.read_resource("semantic-rails://capabilities")["text"])
    tools = list_tool_definitions()
    assert resource["tools"] == [
        {"name": tool["name"], "title": tool["annotations"]["title"]} for tool in tools
    ]
    assert {row["uri"] for row in resource["resources"]} == {
        "semantic-rails://capabilities",
        "semantic-rails://catalog/summary",
        "semantic-rails://catalog/full",
    }
    assert [row["name"] for row in resource["prompts"]]
