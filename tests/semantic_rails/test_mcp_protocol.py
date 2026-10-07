"""Tool results and resource reads carry compact, deterministic JSON text."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import (
    SemanticLayerMCPAdapter,
    json_text,
)
from semantic_rails.mcp_server import handle_jsonrpc_message


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


def _rpc(adapter: SemanticLayerMCPAdapter, method: str, params: dict[str, Any]) -> Any:
    response = handle_jsonrpc_message(
        adapter, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    assert response is not None and "result" in response, response
    return response["result"]


def test_tool_text_channel_is_compact_json_of_the_structured_content(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    result = _rpc(
        adapter, "tools/call", {"name": "discover", "arguments": {"terms": "revenue by store"}}
    )
    text = result["content"][0]["text"]
    assert text == json_text(result["structuredContent"])
    assert len(text) < len(json.dumps(result["structuredContent"], indent=2, default=str))


def test_resource_text_is_compact(adapter: SemanticLayerMCPAdapter) -> None:
    result = _rpc(adapter, "resources/read", {"uri": "semantic-rails://capabilities"})
    text = result["contents"][0]["text"]
    assert "\n" not in text
    assert json.loads(text)["package_id"] == "jaffle_shop"


def test_json_text_is_deterministic() -> None:
    assert json_text({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
