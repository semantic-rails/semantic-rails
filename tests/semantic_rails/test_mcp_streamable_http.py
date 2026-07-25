from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.mcp import MCP_TOOL_DEFINITIONS
from semantic_rails.mcp_server import MCP_PROTOCOL_VERSION
from semantic_rails.mcp_streamable_http import (
    MCP_MAX_REQUEST_BYTES,
    handle_streamable_http_request,
)
from semantic_rails.request_context import (
    HeaderPolicyContextResolver,
    RequestContext,
    get_audit_sink,
    set_audit_sink,
    set_policy_context_resolver,
)
from semantic_rails.schema import SemanticPolicyConfig

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
}


class RecordingAdapter:
    package_id = "jaffle_shop"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return list(MCP_TOOL_DEFINITIONS)

    def list_resources(self) -> list[dict[str, Any]]:
        return [{"uri": "semantic-rails://capabilities", "name": "capabilities"}]

    def list_prompts(self) -> list[dict[str, Any]]:
        return [{"name": "semantic-rails-query-builder"}]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        request_context=None,
    ) -> dict[str, Any]:
        copied = json.loads(json.dumps(arguments))
        self.calls.append((name, copied))
        return {"ok": True, "tool": name, "arguments": copied}

    def read_resource(self, uri: str, *, request_context=None) -> dict[str, Any]:
        return {"uri": uri, "mimeType": "application/json", "text": "{}"}

    def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "arguments": dict(arguments), "messages": []}


def _request(
    adapter: RecordingAdapter,
    message: dict[str, Any] | list[Any],
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
):
    return handle_streamable_http_request(
        adapter,  # type: ignore[arg-type]
        method=method,
        headers=MCP_HEADERS if headers is None else headers,
        body=json.dumps(message).encode("utf-8"),
    )


def test_streamable_http_initializes_latest_protocol():
    response = _request(
        RecordingAdapter(),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
        },
    )

    assert response.status == 200
    assert response.headers["MCP-Protocol-Version"] == MCP_PROTOCOL_VERSION
    assert response.payload
    assert response.payload["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert response.payload["result"]["capabilities"] == {
        "tools": {},
        "resources": {},
        "prompts": {},
    }


def test_streamable_http_notification_returns_202_without_body():
    response = _request(
        RecordingAdapter(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )

    assert response.status == 202
    assert response.payload is None
    assert response.headers["MCP-Protocol-Version"] == MCP_PROTOCOL_VERSION


@pytest.mark.parametrize(
    ("method", "headers", "body", "expected_status"),
    [
        ("GET", {}, b"", 405),
        (
            "POST",
            {"Content-Type": "text/plain", "Accept": MCP_HEADERS["Accept"]},
            b"{}",
            415,
        ),
        (
            "POST",
            {"Content-Type": "application/json", "Accept": "application/json"},
            b"{}",
            406,
        ),
        (
            "POST",
            {
                **MCP_HEADERS,
                "MCP-Protocol-Version": "2099-01-01",
            },
            b"{}",
            400,
        ),
    ],
)
def test_streamable_http_rejects_invalid_method_and_headers(
    method: str,
    headers: dict[str, str],
    body: bytes,
    expected_status: int,
):
    response = handle_streamable_http_request(
        RecordingAdapter(),  # type: ignore[arg-type]
        method=method,
        headers=headers,
        body=body,
    )

    assert response.status == expected_status
    assert response.headers["MCP-Protocol-Version"] == MCP_PROTOCOL_VERSION
    assert response.payload
    assert response.payload["error"]["code"] == -32600


def test_streamable_http_rejects_disallowed_origin(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_CORS_ORIGINS", "https://semantic-rails.com")
    response = _request(
        RecordingAdapter(),
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**MCP_HEADERS, "Origin": "https://attacker.example"},
    )

    assert response.status == 403
    assert response.payload
    assert response.payload["error"]["code"] == -32003


def test_streamable_http_rejects_large_body_and_jsonrpc_batches():
    large = handle_streamable_http_request(
        RecordingAdapter(),  # type: ignore[arg-type]
        method="POST",
        headers=MCP_HEADERS,
        body=b"x" * (MCP_MAX_REQUEST_BYTES + 1),
    )
    batch = _request(RecordingAdapter(), [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])

    assert large.status == 413
    assert batch.status == 400


@pytest.mark.parametrize("tool_name", [row["name"] for row in MCP_TOOL_DEFINITIONS])
def test_streamable_http_calls_every_advertised_tool(tool_name: str):
    adapter = RecordingAdapter()
    response = _request(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": tool_name,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        },
    )

    assert response.status == 200
    assert response.payload
    assert response.payload["result"]["structuredContent"]["tool"] == tool_name
    assert adapter.calls == [(tool_name, {})]


async def _asgi_mcp_call(
    app: SemanticLayerASGIApp,
    *,
    method: str,
    message: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = json.dumps(message).encode("utf-8") if message is not None else b""
    sent: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message_out: dict[str, Any]) -> None:
        sent.append(message_out)

    await app(
        {
            "type": "http",
            "method": method,
            "path": "/mcp",
            "query_string": b"",
            "headers": [
                (key.lower().encode("latin1"), value.encode("latin1"))
                for key, value in {**MCP_HEADERS, **(extra_headers or {})}.items()
            ],
        },
        receive,
        send,
    )
    start = next(
        message_out for message_out in sent if message_out["type"] == "http.response.start"
    )
    response_body = b"".join(
        message_out.get("body", b"")
        for message_out in sent
        if message_out["type"] == "http.response.body"
    )
    return start["status"], start["headers"], response_body


def test_asgi_exposes_stateless_mcp_with_single_protocol_header():
    app = SemanticLayerASGIApp(package_id="jaffle_shop")
    try:
        status, headers, body = asyncio.run(
            _asgi_mcp_call(
                app,
                method="POST",
                message={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        )
    finally:
        asyncio.run(app.aclose())

    decoded_headers = [
        (key.decode("latin1").lower(), value.decode("latin1")) for key, value in headers
    ]
    payload = json.loads(body)
    assert status == 200
    assert len([value for key, value in decoded_headers if key == "x-request-id"]) == 1
    assert len([value for key, value in decoded_headers if key == "mcp-protocol-version"]) == 1
    assert len(payload["result"]["tools"]) == 13


def test_asgi_mcp_requires_api_key_when_configured(monkeypatch):
    """/mcp must honor the same bearer API keys that guard /api/v1/* — the
    execute tool lives behind this endpoint, so a configured deployment must
    not expose it unauthenticated."""
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-demo-key")
    app = SemanticLayerASGIApp(package_id="jaffle_shop")
    try:
        status, _, body = asyncio.run(
            _asgi_mcp_call(
                app,
                method="POST",
                message={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
                },
            )
        )
        assert status == 401
        payload = json.loads(body)
        assert payload["error"]["message"] == "Missing or invalid bearer API key."

        status, _, _ = asyncio.run(
            _asgi_mcp_call(
                app,
                method="POST",
                message={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
                },
                extra_headers={"authorization": "Bearer test-demo-key"},
            )
        )
        assert status == 200
    finally:
        asyncio.run(app.aclose())


def test_asgi_mcp_uses_one_trusted_context_for_tools_resources_and_audit(monkeypatch):
    """Remote MCP must not let tool arguments overrule authenticated identity.

    Exercise both accepted spoof locations (outer and nested), the catalog
    resource path that previously bypassed the resolver, and both audit
    layers. The trusted ``ops`` audience hides the pre-tax measure; the
    spoofed ``internal`` audience would expose it.
    """

    class IdentityResolver:
        def __init__(self) -> None:
            self.payloads: list[object] = []

        def resolve(self, headers, *, payload=None, request_id=""):
            self.payloads.append(payload)
            return RequestContext(
                request_id=request_id,
                actor="identity-user",
                tenant="tenant-trusted",
                roles=("analyst",),
                environment="production",
                audience="ops",
            )

    class RecordingAuditSink:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def emit(self, payload: dict[str, Any]) -> None:
            self.events.append(dict(payload))

    resolver = IdentityResolver()
    sink = RecordingAuditSink()
    previous_sink = get_audit_sink()
    set_policy_context_resolver(resolver)
    set_audit_sink(sink)
    monkeypatch.setenv("SEMANTIC_RAILS_AUDIT_LOGS", "1")
    app = SemanticLayerASGIApp(package_id="jaffle_shop")
    app.runtime.config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.test.hide_segment_from_ops",
            kind="object_visibility",
            object_ids=["segment.jaffle.high_value_customers"],
            audiences=["ops"],
            action="hidden",
        )
    )
    try:
        status, _, body = asyncio.run(
            _asgi_mcp_call(
                app,
                method="POST",
                message={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "validate",
                        "arguments": {
                            "policy_context": {
                                "actor": "spoofed-outer",
                                "tenant": "tenant-spoofed",
                                "roles": ["debug"],
                                "audience": "internal",
                            },
                            "verbosity": "full",
                            "query": {
                                "version": 1,
                                "select": [
                                    {
                                        "expression": {"measure": "measure.jaffle.order_count"},
                                        "as": "orders",
                                    }
                                ],
                                "policy_context": {
                                    "actor": "spoofed-nested",
                                    "audience": "internal",
                                },
                            },
                        },
                    },
                },
                extra_headers={"X-Semantic-Audience": "also-spoofed"},
            )
        )
        assert status == 200
        structured = json.loads(body)["result"]["structuredContent"]
        assert structured["request_context"] == {
            "request_id": structured["request_id"],
            "actor": "identity-user",
            "tenant": "tenant-trusted",
            "roles": ["analyst"],
            "environment": "production",
            "audience": "ops",
        }
        serialized = json.dumps(structured)
        assert "spoofed-outer" not in serialized
        assert "spoofed-nested" not in serialized
        assert "tenant-spoofed" not in serialized

        status, _, body = asyncio.run(
            _asgi_mcp_call(
                app,
                method="POST",
                message={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "catalog",
                        "arguments": {
                            "verbosity": "compact",
                            "policy_context": {"audience": "internal"},
                        },
                    },
                },
            )
        )
        assert status == 200
        catalog_result = json.loads(body)["result"]["structuredContent"]
        assert catalog_result["request_context"]["audience"] == "ops"
        assert "measure.jaffle.lifetime_spend_before_tax_usd" not in json.dumps(catalog_result)

        status, _, body = asyncio.run(
            _asgi_mcp_call(
                app,
                method="POST",
                message={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/read",
                    "params": {"uri": "semantic-rails://catalog/summary"},
                },
            )
        )
        assert status == 200
        resource_text = json.loads(body)["result"]["contents"][0]["text"]
        resource = json.loads(resource_text)
        assert resource["request_context"]["audience"] == "ops"
        assert "measure.jaffle.lifetime_spend_before_tax_usd" not in resource_text

        status, _, body = asyncio.run(
            _asgi_mcp_call(
                app,
                method="POST",
                message={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "segment-preview",
                        "arguments": {
                            "segment_id": "segment.jaffle.high_value_customers",
                            "policy_context": {"audience": "internal"},
                        },
                    },
                },
            )
        )
        assert status == 200
        segment_result = json.loads(body)["result"]["structuredContent"]
        assert segment_result["ok"] is False
        assert segment_result["error"]["code"] == "POLICY_DENIED"
        assert segment_result["request_context"]["audience"] == "ops"
        assert "rows" not in segment_result

        governed_events = [
            event for event in sink.events if event["event"] in {"mcp_tool", "mcp_jsonrpc"}
        ]
        assert governed_events
        assert all(
            event["request_context"]["tenant"] == "tenant-trusted"
            and event["request_context"]["audience"] == "ops"
            for event in governed_events
        )
        # Remote transports do not give the resolver the caller-controlled
        # JSON-RPC body, so a custom identity resolver cannot accidentally
        # treat policy_context arguments as trusted input.
        assert resolver.payloads and all(payload is None for payload in resolver.payloads)
    finally:
        asyncio.run(app.aclose())
        set_audit_sink(previous_sink)
        set_policy_context_resolver(HeaderPolicyContextResolver())


# --- cross-origin access is opt-in ------------------------------------------
#
# Binding to loopback does not protect this server: a page in the operator's
# own browser can reach 127.0.0.1. With `Access-Control-Allow-Origin: *` as
# the default and auth off by default, any website could read the catalog and
# run queries as the operator. Cross-origin access now has to be configured.


def test_cors_origin_header_defaults_to_no_cross_origin_access(monkeypatch):
    from semantic_rails.http_core import cors_origin_header

    monkeypatch.delenv("SEMANTIC_RAILS_CORS_ORIGINS", raising=False)
    assert cors_origin_header(None) == ""
    assert cors_origin_header("https://attacker.example") == ""


def test_cors_wildcard_still_available_as_an_explicit_choice(monkeypatch):
    from semantic_rails.http_core import cors_origin_header

    monkeypatch.setenv("SEMANTIC_RAILS_CORS_ORIGINS", "*")
    assert cors_origin_header("https://anything.example") == "*"


def test_cors_allow_list_echoes_only_listed_origins(monkeypatch):
    from semantic_rails.http_core import cors_origin_header

    monkeypatch.setenv("SEMANTIC_RAILS_CORS_ORIGINS", "https://a.example, https://b.example")
    assert cors_origin_header("https://b.example") == "https://b.example"
    assert cors_origin_header("https://attacker.example") == ""


def test_streamable_http_rejects_any_browser_origin_when_unconfigured(monkeypatch):
    """The Origin guard was previously dead: it tested the truthiness of
    `cors_origin_header`, which returned the string "*" by default."""
    monkeypatch.delenv("SEMANTIC_RAILS_CORS_ORIGINS", raising=False)
    response = _request(
        RecordingAdapter(),
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**MCP_HEADERS, "Origin": "https://attacker.example"},
    )

    assert response.status == 403
    assert response.payload
    assert response.payload["error"]["code"] == -32003


def test_streamable_http_still_serves_clients_that_send_no_origin(monkeypatch):
    """Non-browser MCP clients (Claude Desktop, curl) send no Origin."""
    monkeypatch.delenv("SEMANTIC_RAILS_CORS_ORIGINS", raising=False)
    response = _request(
        RecordingAdapter(),
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers=MCP_HEADERS,
    )

    assert response.status == 200
