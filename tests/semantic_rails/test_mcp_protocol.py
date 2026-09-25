"""MCP text channels are compact, and the SDK facade selects either module shape.

Hosts that forward ``content[].text`` to the model paid for indented JSON:
about half again the size of the same payload in ``structuredContent``. Tool
results and resource reads now carry compact JSON. The optional stdio facade
selects ``MCPServer`` for a simulated SDK 2.x module, and ``FastMCP`` for
the SDK 1.x module shape. This does not qualify an installed SDK 2.x package.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import (
    MCP_SERVER_INSTRUCTIONS,
    SemanticLayerMCPAdapter,
    create_optional_fastmcp_server,
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


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, **submodules: str) -> dict[str, Any]:
    """Install a fake ``mcp`` package whose server submodules export ``submodules``."""

    created: dict[str, Any] = {}

    def fake_server(class_name: str) -> type:
        class FakeServer:
            def __init__(self, name: str, instructions: str | None = None) -> None:
                created.update(cls=class_name, name=name, instructions=instructions, tools=[])

            def add_tool(self, fn: Any, **kwargs: Any) -> None:
                created["tools"].append((kwargs["name"], fn))

        FakeServer.__name__ = class_name
        return FakeServer

    mcp_module = types.ModuleType("mcp")
    mcp_module.__path__ = []  # type: ignore[attr-defined]
    server_module = types.ModuleType("mcp.server")
    server_module.__path__ = []  # type: ignore[attr-defined]
    mcp_module.server = server_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    # A real SDK module another test already imported must not win over the fake.
    for module_name in {"fastmcp", "mcpserver"} - set(submodules):
        monkeypatch.setitem(sys.modules, f"mcp.server.{module_name}", None)
    for module_name, class_name in submodules.items():
        module = types.ModuleType(f"mcp.server.{module_name}")
        setattr(module, class_name, fake_server(class_name))
        setattr(server_module, module_name, module)
        monkeypatch.setitem(sys.modules, f"mcp.server.{module_name}", module)
    return created


@pytest.mark.parametrize(
    ("submodules", "expected"),
    [
        # SDK 2.x renamed FastMCP to MCPServer; mcp.server.fastmcp only raises.
        ({"mcpserver": "MCPServer"}, "MCPServer"),
        ({"fastmcp": "FastMCP"}, "FastMCP"),
    ],
)
def test_facade_uses_whichever_sdk_is_installed(
    adapter: SemanticLayerMCPAdapter,
    monkeypatch: pytest.MonkeyPatch,
    submodules: dict[str, str],
    expected: str,
) -> None:
    created = _install_fake_sdk(monkeypatch, **submodules)
    facade = create_optional_fastmcp_server(adapter)
    assert created["cls"] == expected
    assert created["instructions"] == MCP_SERVER_INSTRUCTIONS
    names = [name for name, _fn in created["tools"]]
    assert names == ["discover", "inspect", "valid-values", "plan", "execute", "segment"]
    text = dict(created["tools"])["discover"]({})
    assert "\n" not in text and json.loads(text)["ok"] is True
    with pytest.raises(RuntimeError, match="stdio-only"):
        facade.run(transport="streamable-http")
