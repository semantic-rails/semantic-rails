"""The query MCP states its workflow once, in the server instructions.

The workflow used to be spread over the thirteen tool descriptions as "loop
position" prose, some of it contradictory (plan said a status-ok draft could
go straight to execute; execute said to run it only after validate and
compile). ``initialize`` now returns the workflow and shared conventions as
``instructions``; each description says what its tool does, when to use it and
its one gotcha. The published schemas still advertise and accept
request_id and policy_context, including on tools with closed input schemas.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from semantic_rails.mcp import (
    MCP_SERVER_INSTRUCTIONS,
    SemanticLayerMCPAdapter,
    create_optional_fastmcp_server,
    list_tool_definitions,
)
from semantic_rails.mcp_server import handle_jsonrpc_message

MINIMAL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "discover": {"terms": "revenue"},
    "inspect": {"object_id": "measure.jaffle.revenue_usd"},
    "valid-values": {"dimension_id": "dimension.jaffle_store_name"},
    "plan": {"intent": "revenue by store"},
    "execute": {
        "query": {"version": 2, "select": [{"expression": {"metric": "metric.sales.aov_usd"}}]}
    },
    "segment": {
        "segment_id": "segment.jaffle.high_value_customers",
        "action": "preview",
        "limit": 2,
    },
}


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


def test_initialize_sends_the_workflow_once(adapter: SemanticLayerMCPAdapter) -> None:
    response = handle_jsonrpc_message(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
    )
    instructions = response["result"]["instructions"]
    assert instructions == MCP_SERVER_INSTRUCTIONS
    # Hosts load instructions up front, so they stay short.
    assert len(instructions) <= 2048
    for tool in MINIMAL_ARGUMENTS:
        assert f"{tool}" in instructions, tool
    assert "policy_context" in instructions


def test_descriptions_carry_no_loop_ceremony() -> None:
    for tool in list_tool_definitions():
        assert "loop position" not in tool["description"].lower(), tool["name"]


def test_request_id_and_policy_context_remain_advertised_and_accepted(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    tools = list_tool_definitions()
    assert {tool["name"] for tool in tools} == set(MINIMAL_ARGUMENTS)
    for tool in tools:
        properties = tool["inputSchema"]["properties"]
        assert properties["request_id"]["type"] == "string", tool["name"]
        assert properties["policy_context"]["type"] == "object", tool["name"]
    extra = {"request_id": "req-accepted", "policy_context": {"environment": "development"}}
    for name, arguments in MINIMAL_ARGUMENTS.items():
        response = adapter.call_tool(name, {**arguments, **extra})
        codes = [issue.get("code") for issue in response.get("errors") or []]
        assert "INVALID_MCP_ARGUMENTS" not in codes, (name, codes)
        warnings = [issue.get("code", "") for issue in response.get("warnings") or []]
        assert not [code for code in warnings if code.endswith("_UNKNOWN_ARG")], (name, warnings)
        assert response["request_id"] == "req-accepted", name


@pytest.mark.parametrize("tool_name", ["segment"])
def test_published_closed_schemas_validate_existing_context_arguments(tool_name: str) -> None:
    tool = next(tool for tool in list_tool_definitions() if tool["name"] == tool_name)
    schema = tool["inputSchema"]
    assert schema["additionalProperties"] is False
    validator = Draft202012Validator(schema)
    arguments = {
        **MINIMAL_ARGUMENTS[tool_name],
        "request_id": "old-client",
        "policy_context": {"environment": "development"},
    }
    validator.validate(arguments)
    with pytest.raises(ValidationError):
        validator.validate({**arguments, "unknown_argument": True})


def test_fastmcp_facade_sends_the_instructions(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: dict[str, Any] = {}

    class FakeFastMCP:
        def __init__(self, name: str, instructions: str | None = None) -> None:
            created["name"], created["instructions"] = name, instructions

        def add_tool(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = FakeFastMCP  # type: ignore[attr-defined]
    server_module = types.ModuleType("mcp.server")
    server_module.fastmcp = fastmcp_module  # type: ignore[attr-defined]
    mcp_module = types.ModuleType("mcp")
    mcp_module.server = server_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    # On SDK 2.x the facade prefers mcp.server.mcpserver; keep this test on the fake.
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)

    create_optional_fastmcp_server(adapter)
    assert created["instructions"] == MCP_SERVER_INSTRUCTIONS
