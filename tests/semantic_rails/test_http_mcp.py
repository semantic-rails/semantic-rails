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

    def call(
        backend, message=PING, *, method="POST", path="/mcp", headers=None, raw=None, server=None
    ):
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
                    # The address the connection reached, as uvicorn reports it.
                    "server": server or httpd.server_address,
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
    monkeypatch.delenv("SEMANTIC_RAILS_ALLOWED_HOSTS", raising=False)
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
                        "verbosity": "full",
                        "query": {
                            "select": [
                                {
                                    "expression": "measure.jaffle.order_count",
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
                assert answer["rows"] == baseline["structuredContent"]["rows"]
                assert answer["rows"] and answer["rows"][0]["orders"] > 0
                assert answer["rendered_sql"] == baseline["structuredContent"]["rendered_sql"]


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
        ("TRACE", "/mcp", {}, None, 405),
        ("CONNECT", "/mcp", {}, None, 405),
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
    if status == 404:
        assert json.loads(local[2])["error"] == json.loads(remote[2])["error"]
    else:
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
        def resolve(self, headers, *, request_id=""):
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
            "arguments": {"terms": "", "verbosity": "full"},
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
                    assert answer["error"]["code"] == "INVALID_QUERY"
                    assert "undeclared" in answer["error"]["message"]
    finally:
        set_policy_context_resolver(HeaderPolicyContextResolver())


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Length": "-1"}, 400),
        ({"Content-Length": "invalid"}, 400),
        ({"Transfer-Encoding": "chunked"}, 400),
        ({"Content-Length": str(MCP_MAX_REQUEST_BYTES + 1)}, 413),
    ],
)
def test_serve_refuses_invalid_or_oversized_framing_without_reading(frontends, headers, status):
    assert frontends("serve", headers=headers, raw=b"")[0] == status


def test_serve_authenticates_before_reading_the_body(frontends, monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-key")
    assert frontends("serve", headers={"Content-Length": "1000"}, raw=b"")[0] == 401


def test_serve_head_refuses_streaming(frontends):
    status, headers, body = frontends("serve", method="HEAD")
    assert status == 405 and not body
    assert headers["Allow"] == "POST, OPTIONS"


# --- DNS rebinding: one Host/Origin check for /api/v1 and /mcp -----------------
#
# A page that points its own name at 127.0.0.1 is same-origin with the local
# server in the browser's eyes, so CORS alone does not stop it from calling the
# API. The server refuses a Host that is not loopback (or a listed host) and a
# browser Origin that is not listed. Refused requests send no body: the local
# server refuses before reading one, and unread bytes would reset the socket.

QUERY = {"select": [{"expression": "measure.jaffle.order_count", "as": "orders"}]}
SURFACES = [
    ("GET", "/api/v1/catalog", b""),
    ("POST", "/api/v1/query", json.dumps(QUERY).encode()),
    ("OPTIONS", "/api/v1/query", b""),
    ("POST", "/mcp", json.dumps(PING).encode()),
]


@pytest.mark.parametrize("backend", ["serve", "asgi"])
@pytest.mark.parametrize(("method", "path"), [surface[:2] for surface in SURFACES])
@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Host": "evil.example"}, 421, "HOST_NOT_ALLOWED"),
        ({"Host": "evil.example:8090"}, 421, "HOST_NOT_ALLOWED"),
        ({"Host": "127.0.0.1.evil.example"}, 421, "HOST_NOT_ALLOWED"),
        ({"Host": "localhost@evil.example"}, 421, "HOST_NOT_ALLOWED"),
        ({"Origin": "https://attacker.example"}, 403, "ORIGIN_NOT_ALLOWED"),
        ({"Origin": "null"}, 403, "ORIGIN_NOT_ALLOWED"),
    ],
)
def test_loopback_server_refuses_rebinding_requests(
    frontends, backend, method, path, headers, status, code
):
    got, response_headers, body = frontends(
        backend, method=method, path=path, headers=headers, raw=b""
    )
    assert got == status
    payload = json.loads(body)
    if path == "/mcp":
        assert payload["error"]["code"] == -32003
        assert response_headers[
            "MCP-Protocol-Version" if backend == "serve" else "mcp-protocol-version"
        ]
    else:
        assert payload["errors"][0]["code"] == code
        assert payload["request_id"] == "transport-parity"


@pytest.mark.parametrize("backend", ["serve", "asgi"])
@pytest.mark.parametrize("host", [None, "localhost", "127.0.0.1:8090", "[::1]:8090", "LOCALHOST"])
def test_loopback_hosts_without_origin_are_served(frontends, backend, host):
    headers = {"Host": host} if host else {}
    for method, path, raw in SURFACES:
        expected = 204 if method == "OPTIONS" else 200
        assert frontends(backend, method=method, path=path, headers=headers, raw=raw)[0] == expected


@pytest.mark.parametrize("backend", ["serve", "asgi"])
def test_listed_hosts_and_origins_are_admitted(frontends, monkeypatch, backend):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOWED_HOSTS", "semantic.example.com, API.example.com")
    monkeypatch.setenv("SEMANTIC_RAILS_CORS_ORIGINS", "https://app.example.com")
    headers = {"Host": "api.example.com:8443", "Origin": "https://app.example.com"}
    for method, path, raw in SURFACES:
        got, response_headers, _ = frontends(
            backend, method=method, path=path, headers=headers, raw=raw
        )
        assert got == (204 if method == "OPTIONS" else 200)
        normalized = {k.lower(): v for k, v in response_headers.items()}
        assert normalized["access-control-allow-origin"] == "https://app.example.com"
    refused = {"Host": "x.example"}
    assert frontends(backend, path="/api/v1/query", headers=refused, raw=b"")[0] == 421


def test_server_beyond_loopback_checks_hosts_once_they_are_listed(frontends, monkeypatch):
    exposed = {"server": ("10.0.0.5", 8080), "headers": {"Host": "semantic.example.com"}}
    assert frontends("asgi", QUERY, path="/api/v1/query", **exposed)[0] == 200
    refused = {**exposed, "headers": {"Origin": "https://attacker.example"}}
    assert frontends("asgi", QUERY, path="/api/v1/query", **refused)[0] == 403
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOWED_HOSTS", "api.example.com")
    assert frontends("asgi", QUERY, path="/api/v1/query", **exposed)[0] == 421
    assert frontends("asgi", path="/mcp", **exposed)[0] == 421
