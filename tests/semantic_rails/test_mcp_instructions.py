"""The query MCP states its workflow once, in the server instructions.

The workflow used to be spread over the thirteen tool descriptions as "loop
position" prose, some of it contradictory (plan said a status-ok draft could
go straight to execute; execute said to run it only after validate and
compile). ``initialize`` now returns the workflow and shared conventions as
``instructions``; each description says what its tool does, when to use it and
its one gotcha. Transport fields remain accepted at runtime, including on
tools with closed input schemas, without appearing in the published schemas.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from scripts.mcp_context import approx_tokens, tool_list_sizes
from semantic_rails.mcp import (
    MCP_SERVER_INSTRUCTIONS,
    SemanticLayerMCPAdapter,
    create_optional_fastmcp_server,
    list_tool_definitions,
)
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.runtime import Runtime

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
    assert "policy_context" not in instructions
    assert "No time block reads all history; nothing is filtered by default" in instructions
    assert "Package policies still apply" in instructions


def test_descriptions_carry_no_loop_ceremony() -> None:
    for tool in list_tool_definitions():
        assert "loop position" not in tool["description"].lower(), tool["name"]


@pytest.mark.parametrize("has_segments", [False, True])
def test_package_segments_control_the_tool_registry(
    adapter: SemanticLayerMCPAdapter, has_segments: bool
) -> None:
    config = adapter.runtime.config
    if not has_segments:
        config = replace(config, segments=[])
    runtime = Runtime.from_config(config, source_path=adapter.runtime.source_path)
    served = SemanticLayerMCPAdapter(runtime)
    try:
        tools = served.list_tools()
        names = {tool["name"] for tool in tools}
        assert ("segment" in names) is has_segments
        assert set(served.tool_handlers) == names
        assert ("segment(" in served.instructions) is has_segments
        if has_segments:
            assert tools == list_tool_definitions()
            assert served.instructions == MCP_SERVER_INSTRUCTIONS
        else:
            assert served.call_tool("segment", MINIMAL_ARGUMENTS["segment"])["ok"] is False
            full = tool_list_sizes(list_tool_definitions())
            without = tool_list_sizes(tools)
            segment = tool_list_sizes(
                [next(t for t in list_tool_definitions() if t["name"] == "segment")]
            )
            for metric in ("model_visible_tokens", "wire_tokens"):
                assert full[metric] - without[metric] == segment[metric]
            assert approx_tokens(served.instructions) < approx_tokens(MCP_SERVER_INSTRUCTIONS)
        tools[0]["inputSchema"]["properties"].clear()
        assert served.list_tools()[0]["inputSchema"]["properties"]
    finally:
        served.close()


@pytest.mark.parametrize("mode", ["validate", "sql", "run"])
def test_execute_adds_no_default_time_or_where(adapter: SemanticLayerMCPAdapter, mode: str) -> None:
    query = {"select": [{"as": "orders", "expression": {"measure": "measure.jaffle.order_count"}}]}
    response = adapter.call_tool("execute", {"query": query, "mode": mode, "verbosity": "full"})
    assert response["ok"], response
    normalized = response["query"]
    assert not normalized.get("time")
    assert not normalized.get("where")
    if mode == "run":
        windowed = adapter.call_tool(
            "execute",
            {
                "query": {
                    **query,
                    "time": {
                        "temporal_role": "temporal_role.jaffle_order_time",
                        "start": "1900-01-01",
                        "end": "2100-01-01",
                    },
                }
            },
        )
        assert windowed["ok"] and windowed["rows"] == response["rows"], windowed


def test_request_id_and_policy_context_are_hidden_but_accepted(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    response = handle_jsonrpc_message(
        adapter, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    tools = response["result"]["tools"]
    assert {tool["name"] for tool in tools} == set(MINIMAL_ARGUMENTS)
    for tool in tools:
        properties = tool["inputSchema"]["properties"]
        assert "request_id" not in properties, tool["name"]
        assert "policy_context" not in properties, tool["name"]
    extra = {"request_id": "req-accepted", "policy_context": {"environment": "development"}}
    for name, arguments in MINIMAL_ARGUMENTS.items():
        response = adapter.call_tool(name, {**arguments, **extra})
        codes = [issue.get("code") for issue in response.get("errors") or []]
        assert "INVALID_MCP_ARGUMENTS" not in codes, (name, codes)
        warnings = [issue.get("code", "") for issue in response.get("warnings") or []]
        assert not [code for code in warnings if code.endswith("_UNKNOWN_ARG")], (name, warnings)
        assert response["request_id"] == "req-accepted", name
        assert response["ok"], (name, response)
        assert response["request_context"]["environment"] == "development", name


@pytest.mark.parametrize("tool_name", ["segment"])
def test_published_closed_schemas_describe_only_task_arguments(tool_name: str) -> None:
    tool = next(tool for tool in list_tool_definitions() if tool["name"] == tool_name)
    schema = tool["inputSchema"]
    assert schema["additionalProperties"] is False
    validator = Draft202012Validator(schema)
    arguments = {
        **MINIMAL_ARGUMENTS[tool_name],
        "request_id": "old-client",
        "policy_context": {"environment": "development"},
    }
    validator.validate(MINIMAL_ARGUMENTS[tool_name])
    with pytest.raises(ValidationError):
        validator.validate(arguments)
    with pytest.raises(ValidationError):
        validator.validate({**arguments, "unknown_argument": True})


@pytest.mark.parametrize("tool_name", ["inspect", "valid-values", "plan", "segment"])
def test_missing_arguments_do_not_report_transport_fields_as_unknown(
    adapter: SemanticLayerMCPAdapter, tool_name: str
) -> None:
    tool = next(t for t in adapter.list_tools() if t["name"] == tool_name)
    arguments = {
        **MINIMAL_ARGUMENTS[tool_name],
        "request_id": "missing-argument",
        "policy_context": {"environment": "development"},
    }
    arguments.pop(tool["inputSchema"]["required"][0])
    result = adapter.call_tool(tool_name, arguments)
    assert result["ok"] is False
    error = result["errors"][0]
    assert error["code"] == "INVALID_MCP_ARGUMENTS"
    assert error["details"]["unknown_keys"] == []


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
