"""Query MCP interface v2: six tools served by the v1 handlers.

v2 folds validate and compile into ``execute(mode)`` and the three segment tools
into ``segment(action)``, drops capabilities, catalog and build-options, and
defaults every tool to its smallest response. It is opt-in, by argument or
``SEMANTIC_RAILS_MCP_INTERFACE``; interface v1 stays the default and byte-identical.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.cli.commands.mcp import _mcp_tool_check
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import (
    MCP_DEFAULT_INTERFACE,
    MCP_DEFAULT_MAX_ROWS,
    MCP_INTERFACE_ENV,
    MCP_SERVER_INSTRUCTIONS,
    MCP_SERVER_INSTRUCTIONS_V2,
    SemanticLayerMCPAdapter,
)
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.request_context import RequestContext

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
SEGMENT = "segment.jaffle.high_value_customers"
QUERY = {
    "version": 2,
    "select": [{"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
    "group_by": [STORE],
    "time": {"temporal_role": ORDER_TIME, "grain": "month", "start": "2017-01-01"},
    "order_by": [{"field": "time", "direction": "ASC"}, {"field": STORE, "direction": "ASC"}],
}
# A window without a grain groups by the raw timestamp: thousands of rows.
UNGRAINED = {
    **QUERY,
    "time": {"temporal_role": ORDER_TIME, "start": "2017-01-01"},
    "order_by": [{"field": STORE, "direction": "ASC"}],
}
V2_TOOLS = ["discover", "inspect", "valid-values", "plan", "execute", "segment"]
V1_ONLY_TOOLS = {
    "capabilities",
    "catalog",
    "build-options",
    "validate",
    "compile",
    "segment-validate",
    "segment-explain",
    "segment-preview",
}
VOLATILE = {"request_id", "timing_ms", "api_version", "request_context"}


@pytest.fixture()
def runtime(runtime_factory: Any) -> Iterator[Any]:
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


@pytest.fixture()
def v1(runtime: Any) -> SemanticLayerMCPAdapter:
    return SemanticLayerMCPAdapter(runtime, interface="v1")


@pytest.fixture()
def v2(runtime: Any) -> SemanticLayerMCPAdapter:
    return SemanticLayerMCPAdapter(runtime, interface="v2")


def _stable(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key not in VOLATILE}


def _initialize(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = handle_jsonrpc_message(adapter, message)
    assert response is not None
    result: dict[str, Any] = response["result"]
    return result


def test_the_interface_comes_from_the_argument_then_the_environment(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(MCP_INTERFACE_ENV, raising=False)
    assert SemanticLayerMCPAdapter(runtime).interface == MCP_DEFAULT_INTERFACE == "v1"
    monkeypatch.setenv(MCP_INTERFACE_ENV, " V2 ")
    assert SemanticLayerMCPAdapter(runtime).interface == "v2"
    assert SemanticLayerMCPAdapter(runtime, interface="v1").interface == "v1"
    monkeypatch.setenv(MCP_INTERFACE_ENV, "v3")
    with pytest.raises(SemanticLayerError) as raised:
        SemanticLayerMCPAdapter(runtime)
    assert raised.value.code == "INVALID_CONFIG"
    assert raised.value.details["valid_values"] == ["v1", "v2"]


def test_each_interface_serves_its_own_frozen_contract(
    v1: SemanticLayerMCPAdapter, v2: SemanticLayerMCPAdapter
) -> None:
    for adapter, contract in ((v1, "query_mcp.v1.json"), (v2, "query_mcp.v2.json")):
        manifest = load_contract(contract)
        assert adapter.list_tools() == manifest["tools"]
        assert adapter.list_resources() == manifest["resources"]
        assert adapter.list_prompts() == manifest["prompts"]
        assert manifest["interface_version"] == adapter.interface
    assert [tool["name"] for tool in v2.list_tools()] == V2_TOOLS
    assert _initialize(v1)["serverInfo"]["version"] == "v1"
    assert _initialize(v1)["instructions"] == MCP_SERVER_INSTRUCTIONS
    initialized = _initialize(v2)
    assert initialized["serverInfo"]["version"] == "v2"
    assert initialized["instructions"] == MCP_SERVER_INSTRUCTIONS_V2
    assert len(MCP_SERVER_INSTRUCTIONS_V2) <= 2048


def _changed_paths(before: Any, after: Any, path: str = "") -> Iterator[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            yield from _changed_paths(before.get(key), after.get(key), f"{path}/{key}")
    elif before != after:
        yield path


def test_v2_contract_differs_from_v1_only_as_designed() -> None:
    v1_tools = {tool["name"]: tool for tool in load_contract("query_mcp.v1.json")["tools"]}
    v2_tools = {tool["name"]: tool for tool in load_contract("query_mcp.v2.json")["tools"]}
    assert set(v1_tools) - set(v2_tools) == V1_ONLY_TOOLS
    assert set(v2_tools) - set(v1_tools) == {"segment"}
    envelope = {"/outputSchema/description", "/outputSchema/properties/api_version/const"}
    execute_input = "/inputSchema/properties/"
    # Their Query IR points at v2's execute instead of v1's validate.
    query = f"{execute_input}query/description"
    slim_cards = {"/description", f"{execute_input}verbosity/default", query, *envelope}
    expected = {
        "discover": slim_cards,
        "inspect": slim_cards,
        "valid-values": {query, *envelope},
        "plan": {"/description", f"{execute_input}detail/default", query, *envelope},
        "execute": {
            "/description",
            f"{execute_input}mode",
            f"{execute_input}max_rows/default",
            f"{execute_input}max_rows/description",
            f"{execute_input}verbosity/description",
            # The full Query IR schema moves here from v1's validate, saying end is exclusive.
            f"{execute_input}query/description",
            f"{execute_input}query/properties",
            *envelope,
        },
    }
    changed = {name: set(_changed_paths(v1_tools[name], v2_tools[name])) for name in expected}
    assert changed == expected
    assert v2_tools["plan"]["inputSchema"]["properties"]["detail"]["default"] == "query"
    execute = v2_tools["execute"]["inputSchema"]["properties"]
    assert execute["max_rows"]["default"] == MCP_DEFAULT_MAX_ROWS
    assert execute["mode"]["enum"] == ["run", "validate", "sql"]
    assert "exclusive" in execute["query"]["properties"]["time"]["properties"]["end"]["description"]
    for tool in ("discover", "inspect"):
        assert v2_tools[tool]["inputSchema"]["properties"]["verbosity"]["default"] == "minimal"


def _descriptions(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _descriptions(item)


def test_v2_text_names_only_v2_tools(v2: SemanticLayerMCPAdapter) -> None:
    texts = [MCP_SERVER_INSTRUCTIONS_V2]
    texts += _descriptions([v2.list_tools(), v2.list_prompts(), v2.list_resources()])
    for prompt, arguments in (
        ("semantic-rails-query-builder", {"intent": "revenue by store"}),
        ("semantic-rails-query-review", {"query_json": "{}"}),
        ("semantic-rails-segment-workflow", {"segment_id": SEGMENT}),
    ):
        texts.append(v2.get_prompt(prompt, arguments)["messages"][0]["content"]["text"])
    for text in texts:
        for name in V1_ONLY_TOOLS:
            assert f"'{name}' tool" not in text and f"{name}(" not in text, (name, text)
        for name in V1_ONLY_TOOLS - {"validate"}:
            assert f"'{name}'" not in text and f"`{name}`" not in text, (name, text)


def test_execute_modes_return_what_the_v1_tools_return(
    v1: SemanticLayerMCPAdapter, v2: SemanticLayerMCPAdapter
) -> None:
    pairs = (
        ({"query": QUERY}, "execute", {"query": QUERY, "max_rows": MCP_DEFAULT_MAX_ROWS}),
        ({"query": QUERY, "mode": "validate"}, "validate", {"query": QUERY}),
        ({"query": QUERY, "mode": "sql", "row_format": "columns"}, "compile", {"query": QUERY}),
        # Top-level Query IR passthrough works in every mode.
        ({**QUERY, "mode": "validate"}, "validate", QUERY),
    )
    for v2_arguments, v1_tool, v1_arguments in pairs:
        v2_response = v2.call_tool("execute", v2_arguments)
        assert v2_response["ok"] is True, v2_response["errors"]
        assert v2_response["api_version"] == "v2"
        assert _stable(v2_response) == _stable(v1.call_tool(v1_tool, v1_arguments))


def test_execute_caps_rows_by_default_in_v2_only(
    v1: SemanticLayerMCPAdapter, v2: SemanticLayerMCPAdapter
) -> None:
    capped = v2.call_tool("execute", {"query": UNGRAINED})
    assert capped["truncated"] is True
    assert capped["row_count"] == MCP_DEFAULT_MAX_ROWS
    assert "EXECUTE_ROWS_TRUNCATED" in {warning["code"] for warning in capped["warnings"]}
    assert v2.call_tool("execute", {"query": UNGRAINED, "max_rows": 5})["row_count"] == 5
    assert v1.call_tool("execute", {"query": UNGRAINED})["row_count"] > MCP_DEFAULT_MAX_ROWS


def test_bad_modes_and_actions_are_argument_errors(v2: SemanticLayerMCPAdapter) -> None:
    for tool, arguments, field in (
        ("execute", {"query": QUERY, "mode": "explain"}, "mode"),
        ("segment", {"segment_id": SEGMENT, "action": "run"}, "action"),
        ("segment", {"segment_id": SEGMENT}, "action"),
        ("segment", {"segment_id": SEGMENT, "action": "validate", "mode": "x"}, None),
    ):
        response = v2.call_tool(tool, arguments)
        error = response["errors"][0]
        assert response["ok"] is False and error["code"] == "INVALID_MCP_ARGUMENTS"
        if field:
            assert error["details"]["field"] == field


def test_v1_execute_still_ignores_mode_with_a_warning(v1: SemanticLayerMCPAdapter) -> None:
    response = v1.call_tool("execute", {"query": QUERY, "mode": "validate", "max_rows": 1})
    assert response["ok"] is True and response["row_count"] == 1
    assert "EXECUTE_UNKNOWN_ARG" in {warning["code"] for warning in response["warnings"]}


@pytest.mark.parametrize("action", ["validate", "explain", "preview"])
def test_segment_actions_are_the_v1_segment_tools_at_minimal_verbosity(
    v1: SemanticLayerMCPAdapter, v2: SemanticLayerMCPAdapter, action: str
) -> None:
    # limit only applies to previews, and is accepted with any action.
    arguments = {"segment_id": SEGMENT, "limit": 3}
    v1_tool = f"segment-{action}"
    v1_arguments = arguments if action == "preview" else {"segment_id": SEGMENT}
    for v2_verbosity, v1_verbosity in (({}, {"verbosity": "minimal"}), ({"verbosity": "full"}, {})):
        v2_response = v2.call_tool("segment", {**arguments, "action": action, **v2_verbosity})
        v1_response = v1.call_tool(v1_tool, {**v1_arguments, **v1_verbosity})
        assert v2_response["ok"] is True, v2_response["errors"]
        assert set(v2_response) == set(v1_response)
        # Preview samples rows, and full responses carry timings, so compare counts there.
        if action == "preview" or v2_verbosity:
            for key in ("status", "member_count", "preview_row_count", "derived_query"):
                assert v2_response.get(key) == v1_response.get(key), key
        else:
            assert _stable(v2_response) == _stable(v1_response)


def test_plan_defaults_to_the_compact_query_detail_in_v2(
    v1: SemanticLayerMCPAdapter, v2: SemanticLayerMCPAdapter
) -> None:
    intent = {"intent": "revenue by store"}
    compact = _stable(v2.call_tool("plan", intent))
    assert compact == _stable(v1.call_tool("plan", {**intent, "detail": "query"}))
    assert "intent_ir" not in compact
    assert "intent_ir" in v1.call_tool("plan", intent)
    assert "intent_ir" in v2.call_tool("plan", {**intent, "detail": "best"})


def test_discover_and_inspect_return_slim_cards_by_default_in_v2(
    v1: SemanticLayerMCPAdapter, v2: SemanticLayerMCPAdapter
) -> None:
    for tool, arguments in (
        ("discover", {"terms": "revenue by store"}),
        ("inspect", {"object_id": "measure.jaffle.revenue_usd"}),
    ):
        slim = _stable(v2.call_tool(tool, arguments))
        assert slim == _stable(v1.call_tool(tool, {**arguments, "verbosity": "minimal"}))
        full = v2.call_tool(tool, {**arguments, "verbosity": "compact"})
        assert _stable(full) == _stable(v1.call_tool(tool, arguments))
        assert len(str(slim)) < len(str(full))


def test_discover_with_empty_terms_lists_ids_in_v2(
    v1: SemanticLayerMCPAdapter, v2: SemanticLayerMCPAdapter
) -> None:
    listed = v2.call_tool("discover", {"terms": ""})
    assert listed["ok"] is True and listed["warnings"] == []
    assert listed["catalog"] == v1.call_tool("catalog", {})["catalog"]
    segments = v2.call_tool("discover", {"terms": " ", "kinds": ["segment"]})["catalog"]
    assert SEGMENT in segments["segment_ids"] and "measure_ids" not in segments
    two = v2.call_tool("discover", {"terms": "", "kinds": "segment,metric"})["catalog"]
    assert {key for key in two if key.endswith("_ids")} == {"segment_ids", "metric_ids"}
    # v1 keeps its short ranked browse.
    assert "DISCOVER_NO_TERMS" in {
        warning["code"] for warning in v1.call_tool("discover", {"terms": ""})["warnings"]
    }


@pytest.mark.parametrize("tool", sorted(V1_ONLY_TOOLS))
def test_v1_only_tools_point_to_their_v2_replacement(
    v2: SemanticLayerMCPAdapter, tool: str
) -> None:
    response = v2.call_tool(tool, {})
    error = response["errors"][0]
    assert error["code"] == "UNKNOWN_MCP_TOOL"
    assert error["details"]["available_tools"] == sorted(V2_TOOLS)
    replacement = error["details"]["replacement"]
    assert replacement in error["message"]
    assert error["recovery_hints"][0]["message"] == f"Use {replacement} instead."


def test_mcp_doctor_checks_the_selected_interface(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MCP_INTERFACE_ENV, "v2")
    check = _mcp_tool_check(runtime)
    assert check["interface"] == "v2"
    assert check["required_tools_present"] is True
    assert check["tool_count"] == len(V2_TOOLS)


def test_v2_tools_take_the_trusted_transport_context(v2: SemanticLayerMCPAdapter) -> None:
    for tool in v2.list_tools():
        assert {"request_id", "policy_context"} <= set(tool["inputSchema"]["properties"])
    context = RequestContext(
        request_id="trusted", actor="analyst@example.com", roles=("analyst",), environment="dev"
    )
    seen: dict[str, Any] = {}

    def validate(arguments: dict[str, Any]) -> dict[str, Any]:
        seen.update(arguments)
        return {"ok": True}

    v2._handle_validate = validate  # type: ignore[method-assign]
    claimed = {"roles": ["admin"], "environment": "prod"}
    response = v2.call_tool(
        "execute",
        {"query": {**QUERY, "policy_context": claimed}, "mode": "validate", "request_id": "x"},
        request_context=context,
    )
    assert response["request_id"] == "trusted"
    assert seen["policy_context"] == context.to_policy_context()
    assert "policy_context" not in seen["query"] and "mode" not in seen


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("discover", {"terms": "revenue"}),
        ("discover", {"terms": ""}),
        ("inspect", {"object_id": "measure.jaffle.revenue_usd"}),
        ("valid-values", {"dimension_id": STORE}),
        ("plan", {"intent": "revenue by store"}),
        ("execute", {"query": QUERY}),
        ("execute", {"query": QUERY, "mode": "validate"}),
        ("execute", {"query": QUERY, "mode": "sql"}),
        ("segment", {"segment_id": SEGMENT, "action": "validate"}),
        ("segment", {"segment_id": SEGMENT, "action": "explain"}),
        ("segment", {"segment_id": SEGMENT, "action": "preview", "limit": 2}),
    ],
)
def test_every_v2_call_works_behind_an_authenticated_transport(
    v2: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    context = RequestContext(
        request_id="trusted", actor="analyst@example.com", roles=("analyst",), environment="dev"
    )
    claimed = {**arguments, "request_id": "caller", "policy_context": {"roles": ["admin"]}}
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": claimed},
    }
    response = handle_jsonrpc_message(v2, message, request_context=context)
    assert response is not None
    result = response["result"]
    payload = result["structuredContent"]
    assert result["isError"] is False and payload["ok"] is True, payload.get("errors")
    assert payload["request_id"] == "trusted"
    assert payload["request_context"]["roles"] == ["analyst"]
    assert not [w for w in payload["warnings"] if str(w.get("code", "")).endswith("_UNKNOWN_ARG")]
