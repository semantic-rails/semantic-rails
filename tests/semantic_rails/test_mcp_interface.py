from __future__ import annotations

import json
import sys
import threading
import types
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer
from io import StringIO

import pytest

from semantic_rails.mcp import (
    MCP_PROMPT_DEFINITIONS,
    MCP_RESOURCE_DEFINITIONS,
    MCP_TOOL_DEFINITIONS,
    SemanticLayerMCPAdapter,
    create_optional_fastmcp_server,
)
from semantic_rails.mcp_server import handle_jsonrpc_message, make_mcp_http_handler, serve_stdio
from semantic_rails.request_context import (
    HeaderPolicyContextResolver,
    RequestContext,
    set_policy_context_resolver,
)

REQUIRED_TOOL_NAMES = {
    "capabilities",
    "catalog",
    "discover",
    "inspect",
    "build-options",
    "valid-values",
    "plan",
    "validate",
    "compile",
    "execute",
    "segment-validate",
    "segment-explain",
    "segment-preview",
}


def test_optional_fastmcp_facade_is_strictly_stdio_only(runtime_factory, monkeypatch):
    calls: list[tuple[tuple, dict]] = []

    class FakeFastMCP:
        def __init__(self, _name):
            self.tools = []

        def add_tool(self, tool, **kwargs):
            self.tools.append((tool, kwargs))

        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "stdio-ran"

        def streamable_http_app(self):
            return object()

    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = FakeFastMCP
    server_module = types.ModuleType("mcp.server")
    server_module.fastmcp = fastmcp_module
    mcp_module = types.ModuleType("mcp")
    mcp_module.server = server_module
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        server = create_optional_fastmcp_server(adapter)
        assert server.transport_scope == "stdio-only"
        assert server.run() == "stdio-ran"
        assert calls == [((), {})]
        for invoke in (
            lambda: server.run(transport="streamable-http"),
            lambda: server.run("sse"),
            lambda: server.streamable_http_app(),
        ):
            try:
                invoke()
            except RuntimeError as exc:
                assert "stdio-only" in str(exc)
            else:
                raise AssertionError("FastMCP network transport must be rejected")
    finally:
        adapter.close()


def test_mcp_normalizes_policy_context_once_per_tool_call(runtime_factory, monkeypatch):
    import semantic_rails.mcp as mcp_module

    calls = []
    original = mcp_module.context_from_policy_context

    def counting(context, *, request_id=""):
        calls.append((dict(context or {}), request_id))
        return original(context, request_id=request_id)

    monkeypatch.setattr(mcp_module, "context_from_policy_context", counting)
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        result = adapter.call_tool(
            "catalog",
            {"policy_context": {"audience": "ops"}, "request_id": "one-context"},
        )
    finally:
        adapter.close()

    assert result["ok"] is True
    assert calls == [({"audience": "ops"}, "one-context")]


def _orders_by_store_query() -> dict:
    return {
        "version": 1,
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                },
                "as": "orders",
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "limit": 2,
    }


def test_mcp_definitions_are_declarative_and_complete():
    tool_names = {definition["name"] for definition in MCP_TOOL_DEFINITIONS}

    assert tool_names == REQUIRED_TOOL_NAMES
    assert {definition["uri"] for definition in MCP_RESOURCE_DEFINITIONS} == {
        "semantic-rails://capabilities",
        "semantic-rails://catalog/summary",
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
        success = adapter.call_tool("capabilities", {"request_id": "schema-success"})
        failure = adapter.call_tool("inspect", {"object_id": "measure.does_not_exist"})
        schema = next(
            definition["outputSchema"]
            for definition in MCP_TOOL_DEFINITIONS
            if definition["name"] == "capabilities"
        )
        validator = jsonschema.Draft202012Validator(schema)
        assert not list(validator.iter_errors(success))
        assert not list(validator.iter_errors(failure))
    finally:
        adapter.close()


def test_mcp_adapter_metadata_tools_match_public_v1_payloads(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        # Use ``verbosity=full`` here: this test asserts the alias_index
        # is present, which only ships at ``full`` after round four.
        catalog = adapter.call_tool("catalog", {"verbosity": "full", "request_id": "mcp-catalog"})
        discovered = adapter.tool_handlers["discover"](
            {"terms": "orders by store", "stage": "initial", "limit": 3}
        )
        inspected = adapter.call_tool("inspect", {"object_id": "measure.jaffle.order_count"})
        build_options = adapter.call_tool(
            "build-options",
            {
                "query": {
                    "version": 1,
                    "select": [
                        {
                            "expression": {
                                "measure": "measure.jaffle.order_count",
                                "aggregation": "count_distinct",
                            },
                            "as": "orders",
                        }
                    ],
                },
                "focus_terms": "orders by store",
                "focus_object_id": "measure.jaffle.order_count",
                "limit": 5,
            },
        )
        valid_values = adapter.call_tool(
            "valid-values",
            {"dimension_id": "dimension.jaffle_item_product_type", "search": "drink", "limit": 1},
        )
        planned = adapter.call_tool("plan", {"intent": "new customer orders over time"})

        assert catalog["ok"] is True
        assert catalog["request_id"] == "mcp-catalog"
        assert catalog["api_version"] == "v1"
        assert catalog["package_id"] == "jaffle_shop"
        assert "dimensionjafflestorename" in catalog["catalog"]["alias_index"]
        assert discovered["ok"] is True
        assert discovered["stage"] == "initial"
        assert discovered["measures"]
        assert inspected["card"]["id"] == "measure.jaffle.order_count"
        assert inspected["card"]["usage_summary"]["default_aggregation"] == "count_distinct"
        assert build_options["recommended"][0]["id"] == "dimension.jaffle_store_name"
        assert valid_values["values"][0]["value"] == "beverage"
        assert valid_values["source"] == "value_domain"
        assert planned["status"] == "ok"
        assert planned["best"]["query_ir"]
    finally:
        adapter.close()


def test_mcp_valid_values_requires_explicit_live_lookup(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)

    def _raise_live_query(*args, **kwargs):
        raise AssertionError("valid-values should not query by default")

    def _fake_query(payload):
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

    runtime.query = _raise_live_query
    runtime._get_adapter = _raise_live_query
    try:
        default_payload = adapter.call_tool(
            "valid-values",
            {"dimension_id": "dimension.jaffle_order_customer_id", "query": {"version": 1}},
        )

        runtime.query = _fake_query
        live_payload = adapter.call_tool(
            "valid-values",
            {
                "dimension_id": "dimension.jaffle_order_customer_id",
                "query": {"version": 1},
                "allow_live_query": True,
            },
        )

        assert default_payload["ok"] is True
        assert default_payload["source"] == "none"
        assert default_payload["values"] == []
        assert live_payload["ok"] is True
        assert live_payload["source"] == "duckdb"
        assert live_payload["value_source_type"] == "live_query"
    finally:
        adapter.close()


def test_mcp_adapter_runtime_and_segment_tools(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    query = _orders_by_store_query()

    def _fake_query(payload):
        return {
            "rows": [{"dimension.jaffle_store_name": "Brooklyn", "orders": 42}],
            "row_count": 1,
            "rendered_sql": "select 1",
            "query": dict(payload),
            "normalized_query": {},
        }

    def _fake_segment_preview(segment_id: str, *, limit: int = 50, policy_context=None):
        return {
            "segment": {"id": segment_id},
            "rows": [{"dimension.jaffle_customer_id": "customer_1"}],
            "preview_row_count": 1,
            "member_count": 1,
            "limit": limit,
        }

    runtime.query = _fake_query
    runtime.segment_preview = _fake_segment_preview
    try:
        # The MCP adapter now defaults validate/compile/execute to
        # verbosity='minimal'; this test inspects compact-level fields
        # (normalized_query), so ask for compact explicitly.
        validated = adapter.call_tool("validate", {"query": query, "verbosity": "compact"})
        compiled = adapter.call_tool("compile", {"query": query})
        executed = adapter.call_tool("execute", {"query": query})
        segment_validated = adapter.call_tool(
            "segment-validate", {"segment_id": "segment.jaffle.high_value_customers"}
        )
        segment_explained = adapter.call_tool(
            "segment-explain", {"segment_id": "segment.jaffle.high_value_customers"}
        )
        segment_preview = adapter.call_tool(
            "segment-preview", {"segment_id": "segment.jaffle.high_value_customers", "limit": 3}
        )

        assert validated["ok"] is True
        assert validated["normalized_query"]["group_by"] == ["dimension.jaffle_store_name"]
        assert compiled["rendered_sql"]
        assert executed["row_count"] == 1
        assert executed["query"]["limit"] == 2
        assert (
            segment_validated["normalized_segment"]["id"] == "segment.jaffle.high_value_customers"
        )
        assert segment_explained["segment"]["id"] == "segment.jaffle.high_value_customers"
        assert segment_preview["segment"]["id"] == "segment.jaffle.high_value_customers"
        assert segment_preview["preview_row_count"] == 1
    finally:
        adapter.close()


def test_mcp_adapter_resources_prompts_and_structured_errors(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        capabilities = adapter.read_resource("semantic-rails://capabilities")
        catalog_summary = adapter.read_resource("semantic-rails://catalog/summary")
        prompt = adapter.get_prompt("semantic-rails-query-builder", {"intent": "orders by store"})
        unknown = adapter.call_tool("missing-tool", {})
        invalid_arguments = adapter.call_tool("catalog", "not-a-json-object")  # type: ignore[arg-type]
        invalid_query = adapter.call_tool(
            "validate", {"query": "not-a-json-object", "request_id": "mcp-bad-query"}
        )
        invalid_limit = adapter.call_tool("discover", {"terms": "orders", "limit": "many"})

        assert capabilities["mimeType"] == "application/json"
        assert json.loads(capabilities["text"])["package_id"] == "jaffle_shop"
        assert catalog_summary["payload"]["catalog"]["meta"]["verbosity"] == "compact"
        assert "discover" in prompt["messages"][0]["content"]["text"]
        assert unknown["ok"] is False
        assert unknown["errors"][0]["code"] == "UNKNOWN_MCP_TOOL"
        assert invalid_arguments["ok"] is False
        assert invalid_arguments["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
        assert invalid_query["ok"] is False
        assert invalid_query["request_id"] == "mcp-bad-query"
        assert invalid_query["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
        assert invalid_limit["ok"] is False
        assert invalid_limit["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
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
                    "name": "catalog",
                    "arguments": {"verbosity": "compact", "request_id": "stdio-req"},
                },
            },
        )
        assert called and called["result"]["structuredContent"]["request_id"] == "stdio-req"

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
                "params": {"name": "catalog", "arguments": "bad"},
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


@contextmanager
def _serve_mcp_http(adapter):
    handler = make_mcp_http_handler(adapter)
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_mcp_http_and_sse_transport_smoke(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        with _serve_mcp_http(adapter) as base:
            options_req = urllib.request.Request(base + "/mcp", method="OPTIONS")
            with urllib.request.urlopen(options_req) as resp:  # nosec - local test server only
                assert resp.status == 204
                assert resp.headers["Content-Length"] == "0"
                assert "X-Semantic-API-Key" in resp.headers["Access-Control-Allow-Headers"]

            with urllib.request.urlopen(base + "/sse?request_id=sse-req") as resp:  # nosec - local test server only
                assert resp.headers["Content-Type"] == "text/event-stream"
                assert resp.headers["X-Request-ID"] == "sse-req"
                sse_body = resp.read().decode("utf-8")
                assert "event: endpoint" in sse_body
                assert '"request_id": "sse-req"' in sse_body

            req = urllib.request.Request(
                base + "/mcp?request_id=mcp-query-req",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}).encode(
                    "utf-8"
                ),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:  # nosec - local test server only
                payload = dict(json.loads(resp.read().decode("utf-8")) or {})
                assert resp.headers["X-Request-ID"] == "mcp-query-req"
                assert payload["result"]["resources"]

            notification_req = urllib.request.Request(
                base + "/mcp",
                data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(
                    "utf-8"
                ),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(notification_req) as resp:  # nosec - local test server only
                assert resp.status == 204
                assert resp.read() == b""

            notification_batch_req = urllib.request.Request(
                base + "/mcp",
                data=json.dumps([{"jsonrpc": "2.0", "method": "notifications/initialized"}]).encode(
                    "utf-8"
                ),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(notification_batch_req) as resp:  # nosec - local test server only
                assert resp.status == 204
                assert resp.read() == b""

            batch_req = urllib.request.Request(
                base + "/mcp",
                data=json.dumps([{"jsonrpc": "2.0", "id": 2, "method": "ping"}, 1]).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(batch_req) as resp:  # nosec - local test server only
                batch_payload = list(json.loads(resp.read().decode("utf-8")) or [])
                assert resp.status == 200
                assert batch_payload[0]["result"] == {}
                assert batch_payload[1]["error"]["code"] == -32600

            empty_batch_req = urllib.request.Request(
                base + "/mcp",
                data=b"[]",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(empty_batch_req) as resp:  # nosec - local test server only
                empty_batch_payload = dict(json.loads(resp.read().decode("utf-8")) or {})
                assert resp.status == 200
                assert empty_batch_payload["error"]["code"] == -32600

            scalar_req = urllib.request.Request(
                base + "/mcp",
                data=b"true",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(scalar_req)  # nosec - local test server only
                raise AssertionError("expected scalar JSON body to fail")
            except urllib.error.HTTPError as exc:
                scalar_payload = dict(json.loads(exc.read().decode("utf-8")) or {})
                assert exc.code == 400
                assert scalar_payload["error"]["code"] == -32600
    finally:
        adapter.close()


def test_mcp_http_sse_requires_api_key_when_configured(runtime_factory, monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "mcp-secret")
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        with _serve_mcp_http(adapter) as base:
            with urllib.request.urlopen(base + "/health") as resp:  # nosec - local test server only
                assert resp.status == 200

            try:
                urllib.request.urlopen(base + "/sse")  # nosec - local test server only
                raise AssertionError("expected unauthorized SSE request")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401

            req = urllib.request.Request(
                base + "/sse", headers={"Authorization": "Bearer mcp-secret"}
            )
            with urllib.request.urlopen(req) as resp:  # nosec - local test server only
                assert resp.headers["Content-Type"] == "text/event-stream"
    finally:
        adapter.close()


def test_legacy_mcp_http_uses_resolver_and_strips_spoofed_policy_context(runtime_factory):
    class IdentityResolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            assert payload is None
            return RequestContext(
                request_id=request_id,
                actor="legacy-identity",
                tenant="legacy-tenant",
                roles=("analyst",),
                environment="production",
                audience="ops",
            )

    set_policy_context_resolver(IdentityResolver())
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        with _serve_mcp_http(adapter) as base:
            request = urllib.request.Request(
                base + "/mcp",
                data=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "validate",
                            "arguments": {
                                "policy_context": {
                                    "tenant": "spoofed-outer",
                                    "audience": "internal",
                                },
                                "verbosity": "full",
                                "query": {
                                    **_orders_by_store_query(),
                                    "policy_context": {
                                        "actor": "spoofed-nested",
                                        "audience": "internal",
                                    },
                                },
                            },
                        },
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Semantic-Audience": "spoofed-header",
                },
                method="POST",
            )
            with urllib.request.urlopen(request) as response:  # nosec - local test only
                structured = json.loads(response.read())["result"]["structuredContent"]
            assert structured["request_context"]["actor"] == "legacy-identity"
            assert structured["request_context"]["tenant"] == "legacy-tenant"
            assert structured["request_context"]["audience"] == "ops"
            serialized = json.dumps(structured)
            assert "spoofed-outer" not in serialized
            assert "spoofed-nested" not in serialized
            assert "spoofed-header" not in serialized

            with urllib.request.urlopen(base + "/sse") as response:  # nosec - local test only
                sse = response.read().decode("utf-8")
            assert '"actor": "legacy-identity"' in sse
            assert '"tenant": "legacy-tenant"' in sse
            assert '"audience": "ops"' in sse
    finally:
        adapter.close()
        set_policy_context_resolver(HeaderPolicyContextResolver())


def test_mcp_http_root_serves_route_discovery_banner(runtime_factory):
    """`GET /` returns a 200 with the JSON-RPC endpoint, transports, version,
    and tools_count so a fresh agent doesn't stall on its first call. The
    blind UX walkthrough flagged this surface — agents try `GET /` first
    and used to get a flat 404 with no hint that `/mcp` existed."""
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        with _serve_mcp_http(adapter) as base:
            with urllib.request.urlopen(base + "/") as resp:  # nosec - local test server only
                assert resp.status == 200
                payload = dict(json.loads(resp.read().decode("utf-8")) or {})
            assert payload["service"] == "semantic-rails-mcp"
            assert payload["protocol"] == "jsonrpc-2.0"
            assert payload["package_id"] == adapter.package_id
            assert payload["version"]
            assert isinstance(payload["tools_count"], int)
            assert payload["tools_count"] > 0
            routes = payload["routes"]
            assert "/mcp" in routes
            assert "/sse" in routes
            assert "/health" in routes

            # /health must include the same identifying fields so a probe
            # that already discovered /health gets the route inventory too.
            with urllib.request.urlopen(base + "/health") as resp:  # nosec - local test server only
                health = dict(json.loads(resp.read().decode("utf-8")) or {})
            assert health["ok"] is True
            assert health["service"] == "semantic-rails-mcp"
            assert health["tools_count"] == payload["tools_count"]

            # Unknown GET path must still expose the discovery payload in
            # `data` so agents that miss the banner get the hint on their
            # first error.
            try:
                urllib.request.urlopen(base + "/does-not-exist")  # nosec - local test server only
                raise AssertionError("expected 404 for unknown GET route")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
                body = dict(json.loads(exc.read().decode("utf-8")) or {})
                data = dict(body.get("error", {}).get("data", {}) or {})
                assert data.get("service") == "semantic-rails-mcp"
                assert "/mcp" in dict(data.get("routes", {}) or {})

            # Unknown POST path same contract.
            req = urllib.request.Request(
                base + "/wrong-endpoint",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req)  # nosec - local test server only
                raise AssertionError("expected 404 for unknown POST route")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
                body = dict(json.loads(exc.read().decode("utf-8")) or {})
                data = dict(body.get("error", {}).get("data", {}) or {})
                assert "/mcp" in dict(data.get("routes", {}) or {})
    finally:
        adapter.close()


# ----------------------------------------------------------------------
# MCP error-envelope recovery hints — the whole point of the structured
# envelope is teaching the agent how to retry. Per audit finding I5, the
# common ``INVALID_MCP_ARGUMENTS`` error used to ship
# ``recovery_hints: []`` even though it knew the specific field and
# offending type. These tests pin the hint shape for each known mistake.
# (``inspect`` + typo'd object_id closest-matches is covered separately
# in ``test_inspect_closest_matches.py``.)
# ----------------------------------------------------------------------


def test_mcp_compile_with_string_query_returns_wrap_query_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool("compile", {"query": "this is not a query"})
    finally:
        adapter.close()
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    hints = out["recovery_hints"]
    assert hints, "recovery_hints must be non-empty for INVALID_MCP_ARGUMENTS"
    assert any(h.get("kind") == "wrap_query_as_object" for h in hints)
    wrap_hint = next(h for h in hints if h.get("kind") == "wrap_query_as_object")
    assert "object" in wrap_hint["message"].lower()
    assert wrap_hint["closest_valid_query"]["version"] == 1


def test_mcp_validate_with_string_query_returns_wrap_query_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool("validate", {"query": "give me revenue"})
    finally:
        adapter.close()
    assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    assert out["recovery_hints"], "recovery_hints must be non-empty"


def test_mcp_discover_with_bad_limit_returns_integer_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool("discover", {"terms": "revenue", "limit": "abc"})
    finally:
        adapter.close()
    assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    hints = out["recovery_hints"]
    assert hints
    assert any(h.get("kind") == "use_integer" for h in hints)
    assert any("limit" in h["message"] for h in hints)


def test_mcp_discover_with_object_kinds_returns_array_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool("discover", {"terms": "revenue", "kinds": {"oops": True}})
    finally:
        adapter.close()
    assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    hints = out["recovery_hints"]
    assert hints
    assert any(h.get("kind") == "use_string_or_array" for h in hints)


def test_mcp_unknown_tool_returns_available_tool_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool("hyperdrive", {})
    finally:
        adapter.close()
    assert out["error"]["code"] == "UNKNOWN_MCP_TOOL"
    hints = out["recovery_hints"]
    assert hints
    hint = hints[0]
    assert hint["kind"] == "use_available_tool"
    assert "discover" in hint["available_tools"]
    assert "plan" in hint["available_tools"]


def test_mcp_unknown_prompt_returns_available_prompt_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.get_prompt("not-a-real-prompt", {})
    finally:
        adapter.close()
    assert out["error"]["code"] == "UNKNOWN_MCP_PROMPT"
    hints = out["error"]["recovery_hints"]
    assert hints
    assert hints[0]["kind"] == "use_available_prompt"
    assert hints[0]["available_prompts"]


def test_mcp_unknown_resource_returns_available_resource_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.read_resource("semantic-rails://bogus")
    finally:
        adapter.close()
    payload = out["payload"]
    assert payload["error"]["code"] == "UNKNOWN_MCP_RESOURCE"
    hints = payload["error"]["recovery_hints"]
    assert hints
    assert hints[0]["kind"] == "use_available_resource"
    assert hints[0]["available_resources"]


def test_mcp_jsonrpc_error_envelope_includes_recovery_hints_for_policy_context(runtime_factory):
    """v2 adversarial Attack 5: ``INVALID_MCP_ARGUMENTS`` raised inside
    ``_policy_context_payload`` escapes as a ``SemanticLayerError``, gets
    caught by the JSON-RPC wrapper, and used to ship with empty
    ``recovery_hints`` — even though ``diagnostics.py`` defines the
    hint for that exact argument. The wrapper now runs the same
    enrichment as ``_tool_content`` so the hint reaches the agent.
    """
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        result = handle_jsonrpc_message(
            adapter,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "validate",
                    "arguments": {
                        "query": {"version": 1, "select": []},
                        "policy_context": "prod",
                    },
                },
            },
        )
    finally:
        adapter.close()
    assert result is not None
    error = result["error"]
    assert error["code"] == -32000
    data = error["data"]
    assert data["code"] == "INVALID_MCP_ARGUMENTS"
    hints = list(data.get("recovery_hints", []) or [])
    assert hints, (
        "policy_context: 'prod' must surface a recovery hint — empty "
        "array means the agent has to guess the fix"
    )
    assert any(h.get("kind") == "wrap_policy_context_as_object" for h in hints)
    assert "closest_valid_query" in data


def test_mcp_jsonrpc_error_envelope_populates_top_level_closest_valid_query(runtime_factory):
    """v2 adversarial Attack 5 bonus smell: the top-level
    ``closest_valid_query`` field used to always be ``{}`` even when
    the nested hint carried a real template. Pull it up so agents that
    read the documented envelope find the IR template where the docs
    say they should.
    """
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        result = handle_jsonrpc_message(
            adapter,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "validate",
                    "arguments": {"query": "this is not a query"},
                },
            },
        )
    finally:
        adapter.close()
    structured = result["result"]["structuredContent"]
    assert any(
        dict(h.get("closest_valid_query", {}) or {}) for h in structured.get("recovery_hints", [])
    ), "expected at least one hint to carry a closest_valid_query template"
