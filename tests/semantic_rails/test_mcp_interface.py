from __future__ import annotations

import json
import types
from io import StringIO

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import (
    MCP_PROMPT_DEFINITIONS,
    MCP_RESOURCE_DEFINITIONS,
    MCP_TOOL_DEFINITIONS,
    SemanticLayerMCPAdapter,
)
from semantic_rails.mcp_server import handle_jsonrpc_message, serve_stdio
from semantic_rails.mcp_streamable_http import handle_streamable_http_request
from semantic_rails.request_context import (
    RequestContext,
)

REQUIRED_TOOL_NAMES = {"discover", "inspect", "valid-values", "plan", "execute", "segment"}


@pytest.mark.parametrize("remote", [False, True])
def test_mcp_reads_identity_only_from_the_request_context(runtime_factory, remote):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    context = RequestContext(request_id="one-context", audience="ops") if remote else None
    try:
        answered = adapter.call_tool("discover", {"terms": ""}, request_context=context)
        claimed = adapter.call_tool(
            "discover",
            {"terms": "", "policy_context": {"audience": "ops"}},
            request_context=context,
        )
    finally:
        adapter.close()

    assert answered["ok"] is True
    assert ("request_context" in answered) is remote
    assert claimed["ok"] is False
    assert claimed["errors"][0]["code"] == ("IDENTITY_CLAIM_REFUSED" if remote else "INVALID_QUERY")
    assert claimed["errors"][0]["details"]["path"] == "policy_context"


def test_mcp_definitions_are_declarative_and_complete():
    tool_names = {definition["name"] for definition in MCP_TOOL_DEFINITIONS}

    assert tool_names == REQUIRED_TOOL_NAMES
    assert {definition["uri"] for definition in MCP_RESOURCE_DEFINITIONS} == {
        "semantic-rails://capabilities",
        "semantic-rails://capabilities/summary",
        "semantic-rails://catalog/summary",
        "semantic-rails://catalog/index",
        "semantic-rails://catalog/full",
    }
    assert {definition["name"] for definition in MCP_PROMPT_DEFINITIONS} == {
        "semantic-rails-query-builder",
        "semantic-rails-query-review",
        "semantic-rails-segment-workflow",
    }
    for definition in MCP_TOOL_DEFINITIONS:
        assert definition["inputSchema"]["type"] == "object"
        assert definition["outputSchema"]["type"] == "object"
        assert definition["outputSchema"]["required"]
        assert definition["annotations"]["readOnlyHint"] is True
        assert definition["annotations"]["destructiveHint"] is False
        assert definition["annotations"]["idempotentHint"] is True
        assert "description" in definition
    valid_values_definition = next(
        definition for definition in MCP_TOOL_DEFINITIONS if definition["name"] == "valid-values"
    )
    assert (
        valid_values_definition["inputSchema"]["properties"]["allow_live_query"]["default"] is False
    )


def test_mcp_output_schema_matches_real_success_and_error_envelopes(runtime_factory):
    jsonschema = pytest.importorskip("jsonschema")
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        success = adapter.call_tool("discover", {"terms": "", "request_id": "schema-success"})
        failure = adapter.call_tool("inspect", {"object_id": "measure.does_not_exist"})
        schema = next(
            definition["outputSchema"]
            for definition in MCP_TOOL_DEFINITIONS
            if definition["name"] == "discover"
        )
        validator = jsonschema.Draft202012Validator(schema)
        assert not list(validator.iter_errors(success))
        assert not list(validator.iter_errors(failure))
    finally:
        adapter.close()


def _raise(error: Exception):
    def handler(arguments: dict) -> dict:
        raise error

    return handler


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (lambda arguments: {"object_id": arguments["object_id"]}, None),
        (_raise(SemanticLayerError("ACCESS_DENIED", "denied")), "ACCESS_DENIED"),
        (_raise(KeyError("missing")), "INTERNAL_ERROR"),
    ],
)
def test_replace_tool_handler_swaps_one_adapter_body_behind_the_boundary(
    monkeypatch, handler, code
):
    audited: list[dict] = []
    monkeypatch.setattr(
        "semantic_rails.mcp.emit_audit_event", lambda event, **fields: audited.append(fields)
    )
    runtime = types.SimpleNamespace(
        package_id="host-test",
        close=lambda: None,
        config=types.SimpleNamespace(
            package=types.SimpleNamespace(package_id="host-test"),
            measures=[],
            dimensions=[],
            segments=[],
            semantic_policies=[],
        ),
        snapshot=types.SimpleNamespace(has_segments=False),
    )
    runtime._config = runtime.config
    adapter, other = SemanticLayerMCPAdapter(runtime), SemanticLayerMCPAdapter(runtime)
    seen: list[dict] = []

    def recording(arguments: dict) -> dict:
        seen.append(arguments)
        return handler(arguments)

    adapter.replace_tool_handler("inspect", recording)
    trusted = RequestContext(request_id="trusted", tenant="tenant-a", roles=("analyst",))
    spoofed = {"object_id": "measure.x", "policy_context": {"tenant": "tenant-b"}}

    response = adapter.call_tool("inspect", {"object_id": "measure.x"}, request_context=trusted)
    claimed = adapter.call_tool("inspect", spoofed, request_context=trusted)
    missing = adapter.call_tool("inspect", {}, request_context=trusted)

    # The handler sees the tool's arguments, never an identity; a claim never reaches it.
    assert seen == [{"object_id": "measure.x", "request_id": "trusted"}]
    assert response["ok"] is (code is None)
    assert [issue["code"] for issue in response.get("errors", [])] == ([code] if code else [])
    assert response["request_context"]["tenant"] == "tenant-a"
    assert [(row["tool"], row["request_context"]["tenant"]) for row in audited] == [
        ("inspect", "tenant-a"),
        ("inspect", "tenant-a"),
        ("inspect", "tenant-a"),
    ]
    assert audited[0]["error_codes"] == ([code] if code else [])
    assert claimed["errors"][0]["code"] == "IDENTITY_CLAIM_REFUSED"
    assert missing["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
    assert other.tool_handlers["inspect"] == other._handle_inspect  # noqa: SLF001
    with pytest.raises(ValueError, match="Unknown MCP tool 'no-such-tool'"):
        adapter.replace_tool_handler("no-such-tool", handler)
    assert set(adapter.tool_handlers) == REQUIRED_TOOL_NAMES


class _HostAdapter:
    """A host's own adapter: its own parameter names, no ``interface`` or ``instructions``."""

    package_id = "host-package"

    def list_tools(self):
        return [{"name": "echo", "inputSchema": {"type": "object"}}]

    def call_tool(self, tool, args, *, request_context=None):
        return {"ok": True, "tool": tool, "args": args, "tenant": request_context.tenant}

    def list_resources(self):
        return []

    def read_resource(self, uri, *, request_context=None):
        return {"uri": uri, "mimeType": "application/json", "text": "{}"}

    def list_prompts(self):
        return []

    def get_prompt(self, name, arguments):
        return {"messages": []}


def test_a_host_adapter_serves_through_the_dispatcher_and_the_http_handler():
    trusted = RequestContext(request_id="r", tenant="tenant-a")
    call = {"name": "echo", "arguments": {"x": 1}}
    initialized = handle_jsonrpc_message(
        _HostAdapter(), {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    listed = handle_jsonrpc_message(
        _HostAdapter(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    over_http = handle_streamable_http_request(
        _HostAdapter(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        body=json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call}
        ).encode(),
        request_context=trusted,
    )

    assert initialized["result"]["serverInfo"]["version"] == "v2"
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["echo"]
    assert over_http.status == 200
    assert over_http.payload["result"]["structuredContent"] == {
        "ok": True,
        "tool": "echo",
        "args": {"x": 1},
        "tenant": "tenant-a",
    }


def test_mcp_adapter_metadata_tools_match_public_v1_payloads(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        catalog = adapter.call_tool("discover", {"terms": "", "request_id": "mcp-catalog"})
        discovered = adapter.tool_handlers["discover"](
            {"terms": "orders by store", "stage": "initial", "limit": 3}
        )
        inspected = adapter.call_tool("inspect", {"object_id": "measure.jaffle.order_count"})
        valid_values = adapter.call_tool(
            "valid-values",
            {"dimension_id": "dimension.jaffle_item_product_type", "search": "drink", "limit": 1},
        )
        planned = adapter.call_tool("plan", {"intent": "new customer orders over time"})

        assert catalog["ok"] is True
        assert catalog["request_id"] == "mcp-catalog"
        assert catalog["api_version"] == "v2"
        assert catalog["package_id"] == "jaffle_shop"
        assert "dimension.jaffle_store_name" in catalog["catalog"]["dimension_ids"]
        assert discovered["ok"] is True
        assert "stage" not in discovered  # an echo of the call
        assert discovered["measures"]
        assert inspected["card"]["id"] == "measure.jaffle.order_count"
        assert inspected["card"]["default_aggregation"] == "count_distinct"
        assert valid_values["values"][0]["value"] == "beverage"
        assert valid_values["source"] == "value_domain"
        assert planned["status"] == "ok"
        assert planned["best"]["query"]
    finally:
        adapter.close()


def test_mcp_valid_values_requires_explicit_live_lookup(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)

    def _raise_live_query(*args, **kwargs):
        raise AssertionError("valid-values should not query by default")

    def _fake_query(payload, **_options):
        dimension = list(payload.get("group_by", []) or ["dimension.jaffle_order_customer_id"])[0]
        return {
            "rows": [{dimension: "customer_1", "anchor": 1}],
            "row_count": 1,
            "rendered_sql": "select 1",
            "logical_plan": {},
            "sql_plan": {},
            "explain": {},
            "query": dict(payload),
            "normalized_query": {},
        }

    # The live lookup is an engine sub-query.
    runtime.query_internal = _raise_live_query
    runtime._get_adapter = _raise_live_query
    try:
        default_payload = adapter.call_tool(
            "valid-values",
            {"dimension_id": "dimension.jaffle_order_customer_id", "query": {"version": 1}},
        )

        runtime.query_internal = _fake_query
        live_payload = adapter.call_tool(
            "valid-values",
            {
                "dimension_id": "dimension.jaffle_order_customer_id",
                "query": {"version": 1},
                "allow_live_query": True,
            },
        )

        assert default_payload["ok"] is False
        assert default_payload["status"] == "needs_live_query"
        assert default_payload["next_call"]["arguments"]["allow_live_query"] is True
        assert default_payload["source"] == "none"
        assert default_payload["values"] == []
        assert live_payload["ok"] is True
        assert live_payload["source"] == "duckdb"
        assert live_payload["value_source_type"] == "live_query"
    finally:
        adapter.close()


def test_mcp_adapter_resources_prompts_and_structured_errors(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        capabilities = adapter.read_resource("semantic-rails://capabilities")
        prompt = adapter.get_prompt("semantic-rails-query-builder", {"intent": "orders by store"})
        invalid_arguments = adapter.call_tool("discover", "not-a-json-object")  # type: ignore[arg-type]
        invalid_query = adapter.call_tool(
            "execute",
            {"mode": "validate", "query": "not-a-json-object", "request_id": "mcp-bad-query"},
        )
        invalid_limit = adapter.call_tool("discover", {"terms": "orders", "limit": "many"})

        assert capabilities["mimeType"] == "application/json"
        assert json.loads(capabilities["text"])["package_id"] == "jaffle_shop"
        assert "discover" in prompt["messages"][0]["content"]["text"]
        assert invalid_arguments["ok"] is False
        assert invalid_arguments["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
        assert invalid_query["ok"] is False
        assert invalid_query["request_id"] == "mcp-bad-query"
        assert invalid_query["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
        # Each argument error carries the fix: wrap the query as an object, send an integer.
        hints = {hint["kind"]: hint for hint in invalid_query["errors"][0]["recovery_hints"]}
        assert hints["wrap_query_as_object"]["closest_valid_query"] == {
            "select": ["<measure or metric id>"]
        }
        assert invalid_limit["ok"] is False
        assert invalid_limit["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
        hints = invalid_limit["errors"][0]["recovery_hints"]
        assert any(h["kind"] == "use_integer" and "limit" in h["message"] for h in hints)
    finally:
        adapter.close()


def test_mcp_stdio_jsonrpc_lists_and_calls_tools(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        listed = handle_jsonrpc_message(
            adapter, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert listed and listed["result"]["tools"]

        called = handle_jsonrpc_message(
            adapter,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "discover",
                    "arguments": {"terms": "", "request_id": "stdio-req"},
                },
            },
        )
        assert called and called["result"]["structuredContent"]["request_id"] == "stdio-req"
        assert called["result"]["structuredContent"]["ok"] is True

        bad_version = handle_jsonrpc_message(adapter, {"jsonrpc": "1.0", "id": 4, "method": "ping"})
        bad_params = handle_jsonrpc_message(
            adapter, {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": "bad"}
        )
        bad_arguments = handle_jsonrpc_message(
            adapter,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "discover", "arguments": "bad"},
            },
        )

        assert bad_version and bad_version["error"]["code"] == -32600
        assert bad_params and bad_params["error"]["code"] == -32602
        assert bad_arguments and bad_arguments["error"]["code"] == -32602

        input_stream = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"}) + "\n"
        )
        output_stream = StringIO()
        serve_stdio(adapter, input_stream=input_stream, output_stream=output_stream)
        response = json.loads(output_stream.getvalue())
        assert response["id"] == 3
        assert response["result"]["prompts"]

        notification_stream = StringIO(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        notification_output = StringIO()
        serve_stdio(adapter, input_stream=notification_stream, output_stream=notification_output)
        assert notification_output.getvalue() == ""
    finally:
        adapter.close()
