"""The local server and ASGI share the authenticated Streamable HTTP boundary."""

from __future__ import annotations

import asyncio
import copy
import http.client
import json
import threading
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace

import pytest

from semantic_rails.api import Handler
from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import MCP_PROTOCOL_VERSION, serve_stdio
from semantic_rails.mcp_streamable_http import MCP_MAX_REQUEST_BYTES
from semantic_rails.request_context import (
    HeaderPolicyContextResolver,
    RequestContext,
    set_policy_context_resolver,
)
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "X-Request-ID": "transport-parity",
}
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@contextmanager
def _frontends(runtime):
    from http.server import ThreadingHTTPServer

    app = SemanticLayerASGIApp(path=runtime.source_path)
    app.mcp_adapter.close()
    app.runtime = runtime
    app.mcp_adapter = SemanticLayerMCPAdapter(runtime)

    class LocalHandler(Handler):
        state = SimpleNamespace(
            runtime=runtime, package_id=runtime.package_id, mcp_adapter=app.mcp_adapter
        )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def call(backend, message=PING, *, method="POST", path="/mcp", headers=None, raw=None):
        body = raw if raw is not None else json.dumps(message).encode()
        request_headers = {**HEADERS, **(headers or {})}
        if backend == "serve":
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                connection.request(method, path, body=body, headers=request_headers)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        async def request():
            sent = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(event):
                sent.append(event)

            await app(
                {
                    "type": "http",
                    "method": method,
                    "path": path,
                    "headers": [
                        (k.lower().encode(), v.encode()) for k, v in request_headers.items()
                    ],
                },
                receive,
                send,
            )
            status = sent[0]["status"]
            response_headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
            return status, response_headers, b"".join(event.get("body", b"") for event in sent[1:])

        return asyncio.run(request())

    try:
        yield call
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        asyncio.run(app.aclose())


@pytest.fixture
def frontends(runtime_factory, monkeypatch):
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEYS", raising=False)
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEY_FILE", raising=False)
    monkeypatch.delenv("SEMANTIC_RAILS_CORS_ORIGINS", raising=False)
    with _frontends(runtime_factory("jaffle_shop")) as call:
        yield call


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "tools"),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "request_id": "transport-parity",
                        "query": {
                            "version": 1,
                            "select": [
                                {
                                    "expression": {"measure": "measure.jaffle.order_count"},
                                    "as": "orders",
                                }
                            ],
                        },
                    },
                },
            },
            "structuredContent",
        ),
    ],
)
def test_serve_tools_match_asgi_and_stdio(runtime_factory, monkeypatch, message, expected):
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEYS", raising=False)
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEY_FILE", raising=False)
    runtime = runtime_factory("jaffle_shop")
    stdio = StringIO()
    serve_stdio(
        SemanticLayerMCPAdapter(runtime),
        input_stream=StringIO(json.dumps(message) + "\n"),
        output_stream=stdio,
    )
    baseline = json.loads(stdio.getvalue())["result"]
    with _frontends(runtime) as call:
        for backend in ("serve", "asgi"):
            status, _, body = call(backend, message)
            result = json.loads(body)["result"]
            assert status == 200
            if expected == "tools":
                assert result == baseline
            else:
                answer = result["structuredContent"]
                assert answer["ok"] is True
                assert answer["result"]["rows"] == baseline["structuredContent"]["result"]["rows"]
                assert (
                    answer["result"]["rendered_sql"]
                    == baseline["structuredContent"]["result"]["rendered_sql"]
                )


@pytest.mark.parametrize("backend", ["serve", "asgi"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer test-key"])
def test_serve_and_asgi_require_configured_keys(frontends, monkeypatch, backend, authorization):
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-key")
    headers = {"Authorization": authorization} if authorization else {}
    status, _, body = frontends(backend, headers=headers)
    assert status == (200 if authorization == "Bearer test-key" else 401)
    if status == 401:
        assert json.loads(body) == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32001, "message": "Missing or invalid bearer API key."},
        }
    assert frontends(backend, method="GET", path="/health")[0] == 200


@pytest.mark.parametrize(
    ("method", "path", "headers", "raw", "status"),
    [
        ("POST", "/mcp/", {}, None, 200),
        ("GET", "/mcp", {}, None, 405),
        ("DELETE", "/mcp", {}, None, 405),
        ("PUT", "/mcp", {}, None, 405),
        ("PATCH", "/mcp", {}, None, 405),
        ("POST", "/sse", {}, None, 404),
        ("GET", "/sse", {}, None, 404),
        ("OPTIONS", "/mcp", {}, None, 204),
        ("POST", "/mcp", {"Origin": "https://attacker.example"}, None, 403),
        ("GET", "/mcp", {"Origin": "https://attacker.example"}, None, 403),
        ("OPTIONS", "/mcp", {"Origin": "https://attacker.example"}, None, 403),
        ("POST", "/mcp", {"Content-Type": "text/plain"}, None, 415),
        ("POST", "/mcp", {"Accept": "application/json"}, None, 406),
        ("POST", "/mcp", {"MCP-Protocol-Version": "unknown"}, None, 400),
        ("POST", "/mcp", {}, b"x" * (MCP_MAX_REQUEST_BYTES + 1), 413),
        ("POST", "/mcp", {}, b"\xff", 400),
        ("POST", "/mcp", {}, b"[{}]", 400),
        ("POST", "/mcp", {}, b'{"jsonrpc":"2.0","method":"notifications/initialized"}', 202),
    ],
)
def test_serve_matches_asgi_transport_refusals(frontends, method, path, headers, raw, status):
    local = frontends("serve", method=method, path=path, headers=headers, raw=raw)
    remote = frontends("asgi", method=method, path=path, headers=headers, raw=raw)
    assert local[0] == remote[0] == status
    assert local[2] == remote[2]
    for response_headers in (local[1], remote[1]):
        normalized = {k.lower(): v for k, v in response_headers.items()}
        assert normalized["x-request-id"] == "transport-parity"
        if path.startswith("/mcp"):
            assert normalized["mcp-protocol-version"] == MCP_PROTOCOL_VERSION
            assert "mcp-session-id" not in normalized
            if status == 405:
                assert normalized["allow"] == "POST, OPTIONS"


@pytest.mark.parametrize("environment", ["production", "undeclared"])
def test_serve_preserves_trusted_visibility_and_environment(
    runtime_factory, monkeypatch, environment
):
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEYS", raising=False)
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEY_FILE", raising=False)
    runtime = runtime_factory("jaffle_shop")
    config = copy.deepcopy(runtime.config)
    hidden = "measure.jaffle.revenue_usd"
    config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.test.hidden",
            kind="object_visibility",
            object_ids=[hidden],
            audiences=["ops"],
            action="hidden",
        )
    )
    scoped = Runtime.from_config(
        config, source_path=runtime.source_path, package_id=runtime.package_id
    )

    class Resolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            assert payload is None
            return RequestContext(
                request_id=request_id, actor="trusted", audience="ops", environment=environment
            )

    set_policy_context_resolver(Resolver())
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "discover",
            "arguments": {
                "terms": "",
                "verbosity": "full",
                "policy_context": {
                    "audience": "internal",
                    "environment": "production",
                },
            },
        },
    }
    try:
        with _frontends(scoped) as call:
            answers = [
                json.loads(call(backend, message)[2])["result"]["structuredContent"]
                for backend in ("serve", "asgi")
            ]
            for answer in answers:
                if environment == "production":
                    assert answer["ok"] is True
                    assert hidden not in json.dumps(answer)
                else:
                    assert answer["ok"] is False
                    assert answer["error"]["code"] == "POLICY_ENVIRONMENT_UNDECLARED"
    finally:
        set_policy_context_resolver(HeaderPolicyContextResolver())
