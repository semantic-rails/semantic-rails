"""The local server and ASGI share the authenticated Streamable HTTP boundary."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import http.client
import ipaddress
import json
import socket
import threading
import time
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
import uvicorn

from semantic_rails.api import Handler
from semantic_rails.asgi import PackageLoadFailureApp, SemanticLayerASGIApp
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
from tests.semantic_rails.conftest import copy_package_config, opened

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "X-Request-ID": "transport-parity",
}
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


class _BodyReads:
    """A request stream that records body reads (``read``), not header lines (``readline``)."""

    def __init__(self, stream, reads):
        self._stream, self._reads = stream, reads

    def read(self, size=-1):
        if size:
            self._reads.append("serve")
        return self._stream.read(size)

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextmanager
def _uvicorn(app, *, root_path=""):
    """Serve ``app`` with uvicorn's h11 parser on loopback; yield its address and body reads."""
    reads = []

    async def recording(scope, receive, send):
        async def counted():
            reads.append("asgi")
            return await receive()

        await app(scope, counted, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(
        recording, http="h11", lifespan="off", log_level="warning", root_path=root_path
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield sock.getsockname(), reads
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def _raw(address, request):
    """Send raw request bytes; return the status and body the server answers."""
    with socket.create_connection(address, timeout=5) as connection:
        connection.sendall(request)
        response = b""
        while chunk := connection.recv(65536):
            response += chunk
    head, _, body = response.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


@contextmanager
def _frontends(runtime):
    from http.server import ThreadingHTTPServer

    app = SemanticLayerASGIApp(path=runtime.source_path)
    app.mcp_adapter.close()
    app.runtime = runtime
    app.mcp_adapter = SemanticLayerMCPAdapter(runtime)
    reads = []  # requests whose body either transport read

    class LocalHandler(Handler):
        state = SimpleNamespace(
            runtime=runtime, package_id=runtime.package_id, mcp_adapter=app.mcp_adapter
        )

        def setup(self):
            super().setup()
            self.rfile = _BodyReads(self.rfile, reads)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def call(
        backend, message=PING, *, method="POST", path="/mcp", headers=None, raw=None, server=None
    ):
        """Send one request; ``headers`` is a dict or raw pairs, and a None value omits one."""
        body = raw if raw is not None else json.dumps(message).encode()
        extra = list(headers.items() if isinstance(headers, dict) else headers or [])
        named = {name.lower() for name, _ in extra}
        # What http.client sends unasked, so both transports see the same header list.
        defaults = [
            ("Host", "{}:{}".format(*httpd.server_address)),
            ("Accept-Encoding", "identity"),
        ]
        if "transfer-encoding" not in named:
            defaults.append(("Content-Length", str(len(body))))
        request_headers = [
            *(
                (name, value)
                for name, value in [*defaults, *HEADERS.items()]
                if name.lower() not in named
            ),
            *((name, value) for name, value in extra if value is not None),
        ]
        if backend == "serve":
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                for name, value in request_headers:
                    connection.putheader(name, value)
                connection.endheaders(body)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        async def request():
            sent = []

            async def receive():
                reads.append("asgi")
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(event):
                sent.append(event)

            await app(
                {
                    "type": "http",
                    "method": method,
                    "path": path,
                    "headers": [
                        (k.lower().encode("latin1"), v.encode("latin1")) for k, v in request_headers
                    ],
                    # The target and the address the connection reached, as uvicorn reports them.
                    "raw_path": path.partition("?")[0].encode("latin1"),
                    "server": server or httpd.server_address,
                },
                receive,
                send,
            )
            status = sent[0]["status"]
            response_headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
            return status, response_headers, b"".join(event.get("body", b"") for event in sent[1:])

        return asyncio.run(request())

    call.reads, call.app, call.address = reads, app, httpd.server_address
    try:
        yield call
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        asyncio.run(app.aclose())


_SERVER_SETTINGS = (
    "SEMANTIC_RAILS_API_KEYS",
    "SEMANTIC_RAILS_API_KEY_FILE",
    "SEMANTIC_RAILS_CORS_ORIGINS",
    "SEMANTIC_RAILS_ALLOWED_HOSTS",
)


@pytest.fixture
def frontends(runtime_factory, monkeypatch):
    for name in _SERVER_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    with _frontends(runtime_factory("jaffle_shop")) as call:
        yield call


@pytest.fixture(scope="module")
def _shared_frontends(tmp_path_factory):
    root = copy_package_config(tmp_path_factory.mktemp("loopback"), "jaffle_shop", preseed_db=True)
    with pytest.MonkeyPatch.context() as patch:
        for name in _SERVER_SETTINGS:
            patch.delenv(name, raising=False)
        runtime = opened(Runtime.from_path(str(root)))
        try:
            with _frontends(runtime) as call:
                yield call
        finally:
            runtime.close()


@pytest.fixture
def loopback(_shared_frontends):
    """One server pair for the Host and Origin cases that change no setting; no body read yet."""
    _shared_frontends.reads.clear()
    return _shared_frontends


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


# --- DNS rebinding: one Host/Origin check for every response ------------------
#
# A page that points its own name at 127.0.0.1 is same-origin with the local
# server in the browser's eyes, so CORS alone does not stop it from calling the
# API. The server refuses a Host that is not loopback (or a listed host) and a
# browser Origin that is not listed, before it reads a body: refused requests
# announce a body they never send, which the local server would wait for.

QUERY = {"select": [{"expression": "measure.jaffle.order_count", "as": "orders"}]}
SURFACES = [
    ("GET", "/api/v1/catalog", b""),
    ("POST", "/api/v1/query", json.dumps(QUERY).encode()),
    ("OPTIONS", "/api/v1/query", b""),
    ("POST", "/mcp", json.dumps(PING).encode()),
]
UNREAD_BODY = ("Content-Length", "1000")
HOSTILE = [
    *(
        ([("Host", host)], 421, "HOST_NOT_ALLOWED")
        for host in [
            "evil.example",
            "evil.example:8090",
            "127.0.0.1.evil.example",
            "localhost@evil.example",
            "[::1].evil.example",
            "[::1",
            "localhost:notaport",
            "localhost:65536",
            "localhost:80@evil.example",
            "",
            None,
        ]
    ),
    ([("Host", "evil.example"), ("Host", "localhost")], 421, "HOST_NOT_ALLOWED"),
    ([("Host", "localhost"), ("Host", "evil.example")], 421, "HOST_NOT_ALLOWED"),
    ([("Origin", "https://attacker.example")], 403, "ORIGIN_NOT_ALLOWED"),
    ([("Origin", "null")], 403, "ORIGIN_NOT_ALLOWED"),
    ([("Origin", "")], 403, "ORIGIN_NOT_ALLOWED"),
    ([("Origin", "https://attacker.example"), ("Origin", "")], 403, "ORIGIN_NOT_ALLOWED"),
]


def _assert_refused(response, path, status, code):
    got, response_headers, body = response
    assert got == status
    payload = json.loads(body)
    if path.endswith("/mcp"):
        assert payload["error"]["code"] == -32003
        assert {k.lower() for k in response_headers} >= {"mcp-protocol-version"}
    else:
        assert payload["errors"][0]["code"] == code
        # A minimal answer: the refused caller learns neither the package nor an identity.
        assert "package_id" not in payload and "request_context" not in payload


@pytest.mark.parametrize("backend", ["serve", "asgi"])
@pytest.mark.parametrize(("method", "path"), [surface[:2] for surface in SURFACES])
@pytest.mark.parametrize(("headers", "status", "code"), HOSTILE)
def test_loopback_server_refuses_rebinding_requests(
    loopback, backend, method, path, headers, status, code
):
    response = loopback(backend, method=method, path=path, headers=[*headers, UNREAD_BODY], raw=b"")
    _assert_refused(response, path, status, code)
    assert loopback.reads == []


@pytest.mark.parametrize("backend", ["serve", "asgi"])
@pytest.mark.parametrize("host", [None, "localhost", "127.0.0.1:8090", "[::1]:8090", "LOCALHOST"])
def test_loopback_hosts_without_origin_are_served(loopback, backend, host):
    headers = {"Host": host} if host else {}
    for method, path, raw in SURFACES:
        expected = 204 if method == "OPTIONS" else 200
        assert loopback(backend, method=method, path=path, headers=headers, raw=raw)[0] == expected


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
    def status(headers, path="/api/v1/query"):
        return frontends("asgi", QUERY, path=path, headers=headers, server=("10.0.0.5", 8080))[0]

    assert status({"Host": "semantic.example.com"}) == 200
    assert status({"Host": None}) == 200
    assert status({"Origin": "https://attacker.example"}) == 403
    assert status([("Host", "semantic.example.com"), ("Host", "localhost")]) == 421
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOWED_HOSTS", "api.example.com")
    assert status({"Host": "semantic.example.com"}) == 421
    assert status({"Host": "semantic.example.com"}, path="/mcp") == 421
    assert status({"Host": "api.example.com"}) == 200


def test_mapped_loopback_listener_is_protected_on_every_python(frontends, monkeypatch):
    # Python 3.11.0 calls only ::1 loopback, not ::ffff:127.0.0.1.
    monkeypatch.setattr(ipaddress.IPv6Address, "is_loopback", property(lambda self: self._ip == 1))
    mapped = ("::ffff:127.0.0.1", 8090)
    hostile = frontends(
        "asgi", path="/api/v1/query", headers={"Host": "evil.example"}, server=mapped
    )
    assert hostile[0] == 421
    host = {"Host": "[::ffff:127.0.0.1]:8090"}
    assert frontends("asgi", QUERY, path="/api/v1/query", headers=host, server=mapped)[0] == 200


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("http://evil.example:8090/api/v1/catalog", {"Host": "localhost"}, 421),
        ("http://evil.example:8090/api/v1/catalog", {"Host": None}, 421),
        # The stdlib server reads a leading "//" as a path, not an authority.
        ("//evil.example/api/v1/catalog", {"Host": "localhost"}, 404),
        ("http://[::1/api/v1/catalog", {"Host": "localhost"}, None),
        ("http://evil.example/mcp", {"Host": "localhost"}, 421),
        ("http://localhost:8090/api/v1/catalog", {"Host": "localhost:8090"}, 200),
    ],
)
def test_serve_checks_the_authority_of_an_absolute_target(frontends, path, headers, status):
    if status is None:  # the target cannot be parsed: refused or dropped, never served
        with contextlib.suppress(http.client.HTTPException, ConnectionError):
            assert frontends("serve", method="GET", path=path, headers=headers, raw=b"")[0] >= 400
        return
    response = frontends(
        "serve", method="GET", path=path, headers=[*headers.items(), UNREAD_BODY], raw=b""
    )
    if status == 421:
        _assert_refused(response, path, 421, "HOST_NOT_ALLOWED")
    else:
        assert response[0] == status


def test_serve_refuses_rebinding_for_methods_it_does_not_serve(frontends):
    assert frontends("serve", method="BREW", headers={"Host": "evil.example"}, raw=b"")[0] == 421
    assert frontends("serve", method="BREW", raw=b"")[0] == 501


def test_package_load_failure_refuses_rebinding_before_its_diagnostics():
    app = PackageLoadFailureApp(package_id="broken", error=RuntimeError("/private/path failed"))

    def call(headers):
        sent = []

        async def receive():
            raise AssertionError("a refused request's body is never read")

        async def send(event):
            sent.append(event)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/catalog",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
            "server": ("127.0.0.1", 8080),
        }
        asyncio.run(app(scope, receive, send))
        response_headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
        return sent[0]["status"], response_headers, sent[1]["body"]

    for headers, status in [
        ([("Host", "evil.example"), ("Origin", "https://evil.example")], 421),
        ([("Host", "localhost"), ("Origin", "https://evil.example")], 403),
        ([("Host", "localhost"), ("Host", "evil.example")], 421),
    ]:
        got, response_headers, body = call(headers)
        assert got == status
        assert b"/private/path" not in body and b"broken" not in body
        assert "access-control-allow-origin" not in response_headers
    assert call([("Host", "localhost:8080")])[0] == 503


# --- raw requests through the real parsers ---------------------------------------
#
# Bytes no well-behaved client sends: an absolute-form target naming another host,
# header lines the stdlib parser stops at (hiding the headers after them), and
# non-HTTP padding around a loopback name. Each is refused before any body is read.

PING_BYTES = json.dumps(PING, separators=(",", ":")).encode()
MCP_LINES = (
    b"Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
    b"Content-Length: %d\r\nConnection: close\r\n" % len(PING_BYTES)
)
RAW_REQUESTS = {
    "absolute target": (
        b"GET http://evil.example:8090/api/v1/catalog HTTP/1.1\r\n"
        b"Host: localhost\r\nConnection: close\r\n\r\n",
        421,
    ),
    "absolute mcp target": (
        b"POST http://evil.example:8090/mcp HTTP/1.1\r\nHost: localhost\r\n"
        + MCP_LINES
        + b"\r\n"
        + PING_BYTES,
        421,
    ),
    "header line without a colon hides later headers": (
        b"POST /mcp HTTP/1.1\r\nHost: localhost\r\n"
        + MCP_LINES
        + b"BadHeader\r\nHost: evil.example\r\nOrigin: https://evil.example\r\n\r\n"
        + PING_BYTES,
        400,
    ),
    "api header line without a colon": (
        b"POST /api/v1/catalog HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        b"Content-Length: 2\r\nConnection: close\r\nBadHeader\r\nHost: evil.example\r\n\r\n{}",
        400,
    ),
    "space before a colon": (
        b"GET /api/v1/catalog HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
        b"Origin : https://evil.example\r\n\r\n",
        400,
    ),
    "non-breaking space padding": (
        b"GET /api/v1/catalog HTTP/1.1\r\nHost: \xa0localhost\xa0\r\nConnection: close\r\n\r\n",
        421,
    ),
    "next-line padding": (
        b"GET /api/v1/catalog HTTP/1.1\r\nHost: \x85localhost\x85\r\nConnection: close\r\n\r\n",
        421,
    ),
}


@pytest.mark.parametrize("backend", ["serve", "asgi"])
@pytest.mark.parametrize("name", RAW_REQUESTS)
def test_raw_requests_are_refused_before_any_body_read(frontends, backend, name):
    request, status = RAW_REQUESTS[name]
    if backend == "serve":
        got, body = _raw(frontends.address, request)
        reads = frontends.reads
    else:
        with _uvicorn(frontends.app) as (address, reads):
            got, body = _raw(address, request)
    assert got == status
    assert b"jaffle" not in body
    assert reads == []


def test_raw_loopback_requests_are_still_served(frontends):
    request = b"GET /api/v1/catalog HTTP/1.1\r\nHost: localhost:1\r\nConnection: close\r\n\r\n"
    assert _raw(frontends.address, request)[0] == 200
    with _uvicorn(frontends.app) as (address, _):
        assert _raw(address, request)[0] == 200


def test_package_load_failure_refuses_an_absolute_target_before_its_diagnostics():
    app = PackageLoadFailureApp(package_id="broken", error=RuntimeError("/private/path failed"))
    request = RAW_REQUESTS["absolute target"][0].replace(
        b"Host: localhost", b"Host: localhost\r\nOrigin: http://localhost"
    )
    with _uvicorn(app) as (address, reads):
        got, body = _raw(address, request)
        assert (
            _raw(
                address,
                b"GET /api/v1/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
            )[0]
            == 503
        )
    assert got == 421
    assert b"/private/path" not in body and b"broken" not in body
    assert reads == []


@pytest.mark.parametrize("which", ["app", "package load failure"])
def test_a_root_path_does_not_hide_an_absolute_target(frontends, which):
    # Uvicorn prefixes the target with the root path: /enginehttp://evil.example:8090/...
    app = frontends.app
    if which == "package load failure":
        app = PackageLoadFailureApp(package_id="broken", error=RuntimeError("/private/path failed"))
    with _uvicorn(app, root_path="/engine") as (address, reads):
        for target in [b"http://evil.example:8090/api/v1/catalog", b"http://evil.example/mcp"]:
            request = b"GET %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n" % target
            got, body = _raw(address, request)
            assert got == 421
            assert b"broken" not in body and b"jaffle" not in body
    assert reads == []


@pytest.mark.parametrize("target", ["/api/v1/catalog?next=http://evil.example/", "/x/http:/y"])
def test_a_url_in_the_query_is_not_an_absolute_target(frontends, target):
    for backend in ("serve", "asgi"):
        assert frontends(backend, method="GET", path=target, raw=b"")[0] in {200, 404}


@pytest.mark.parametrize("backend", ["serve", "asgi"])
@pytest.mark.parametrize("pad", ["\xa0", "\x85"])
def test_origin_padding_beyond_http_whitespace_is_refused(frontends, monkeypatch, backend, pad):
    monkeypatch.setenv("SEMANTIC_RAILS_CORS_ORIGINS", "https://app.example.com")
    padded = {"Origin": f"{pad}https://app.example.com{pad}"}
    assert (
        frontends(backend, method="GET", path="/api/v1/catalog", headers=padded, raw=b"")[0] == 403
    )
    allowed = {"Origin": " https://app.example.com\t"}
    assert (
        frontends(backend, method="GET", path="/api/v1/catalog", headers=allowed, raw=b"")[0] == 200
    )
