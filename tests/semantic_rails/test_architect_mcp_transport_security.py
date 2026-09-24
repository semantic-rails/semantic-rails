"""The Architect MCP's network transports: loopback default, Host/Origin checks, bearer token."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from semantic_rails import architect_mcp
from semantic_rails.architect_mcp import (
    ARCHITECT_TOKEN_ENV,
    ARCHITECT_TOKEN_FILE_ENV,
    ArchitectMCPServer,
    _BearerTokenGate,
    architect_http_app,
    create_architect_mcp_server,
    load_architect_token,
)
from semantic_rails.errors import SemanticLayerError

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "transport-test", "version": "0"},
    },
}


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ARCHITECT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(ARCHITECT_TOKEN_FILE_ENV, raising=False)


@pytest.fixture()
def token() -> str:
    return secrets.token_urlsafe(32)


@contextmanager
def _serve(app: Any, *, family: socket.AddressFamily = socket.AF_INET) -> Iterator[str]:
    """Run ``app`` on a real loopback socket; yield its base URL."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind(("::1" if family == socket.AF_INET6 else "127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        host = "[::1]" if family == socket.AF_INET6 else "127.0.0.1"
        yield f"http://{host}:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@contextmanager
def _architect(
    tmp_path: Path, token: str, *, transport: str = "streamable-http", host: str = "127.0.0.1"
) -> Iterator[tuple[httpx.Client, str]]:
    server = create_architect_mcp_server(workspace_root=tmp_path, host=host)
    app = architect_http_app(server, transport, token)  # type: ignore[arg-type]
    with _serve(app) as base_url, httpx.Client(base_url=base_url, timeout=10) as client:
        yield client, base_url


def _initialize(client: httpx.Client, **headers: str) -> httpx.Response:
    return client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- the command-line entry point -------------------------------------------------


def test_default_bind_is_loopback_and_default_transport_is_stdio(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(architect_mcp, "run_architect_mcp_server", lambda **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["semantic-rails-architect-mcp"])

    architect_mcp.main()

    assert seen["host"] == "127.0.0.1"
    assert seen["transport"] == "stdio"


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_entry_point_serves_only_the_gated_app_on_loopback(
    monkeypatch, tmp_path: Path, token: str, transport: str
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    monkeypatch.setenv(ARCHITECT_TOKEN_ENV, token)
    argv = ["semantic-rails-architect-mcp", "--transport", transport]
    monkeypatch.setattr(sys, "argv", [*argv, "--workspace-root", str(tmp_path)])

    architect_mcp.main()

    assert (served["host"], served["port"]) == ("127.0.0.1", 8010)
    assert [row.cls for row in served["app"].user_middleware] == [_BearerTokenGate]

    async def unauthenticated() -> httpx.Response:
        transport_ = httpx.ASGITransport(app=served["app"])
        async with httpx.AsyncClient(transport=transport_, base_url="http://127.0.0.1:8010") as c:
            return await c.get("/sse" if transport == "sse" else "/mcp")

    assert asyncio.run(unauthenticated()).status_code == 401


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_network_transport_refuses_to_start_without_a_token(
    monkeypatch, capsys, tmp_path: Path, transport: str
) -> None:
    def _must_not_serve(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("served without a token")

    monkeypatch.setattr(uvicorn, "run", _must_not_serve)
    argv = ["semantic-rails-architect-mcp", "--transport", transport]
    monkeypatch.setattr(sys, "argv", [*argv, "--workspace-root", str(tmp_path)])

    with pytest.raises(SystemExit) as excinfo:
        architect_mcp.main()

    assert excinfo.value.code == 2
    assert "requires a bearer token" in capsys.readouterr().err


def test_stdio_needs_no_token_and_ignores_a_bad_one(monkeypatch, tmp_path: Path) -> None:
    ran: list[str] = []
    monkeypatch.setattr(
        ArchitectMCPServer, "run", lambda self, transport="stdio": ran.append(transport)
    )
    monkeypatch.setenv(ARCHITECT_TOKEN_ENV, "short")
    monkeypatch.setenv(ARCHITECT_TOKEN_FILE_ENV, str(tmp_path / "missing.token"))
    monkeypatch.setattr(
        sys, "argv", ["semantic-rails-architect-mcp", "--workspace-root", str(tmp_path)]
    )

    architect_mcp.main()

    assert ran == ["stdio"]


# -- the server object ------------------------------------------------------------


def test_server_apps_refuse_to_build_without_a_token(monkeypatch, tmp_path: Path) -> None:
    """A direct Python caller cannot serve the network transports ungated either."""

    async def _must_not_serve(self: Any) -> None:
        raise AssertionError("served without a token")

    monkeypatch.setattr(uvicorn.Server, "serve", _must_not_serve)
    server = create_architect_mcp_server(workspace_root=tmp_path, host="0.0.0.0")

    for build in (server.sse_app, server.streamable_http_app):
        with pytest.raises(SemanticLayerError, match="require a bearer token"):
            build()
    with pytest.raises(SemanticLayerError, match="require a bearer token"):
        server.run("streamable-http")


@pytest.mark.parametrize("bad", ["", "x", "t" * 31, "has spaces in it but is long enough......"])
def test_gate_refuses_an_empty_short_or_unsendable_token(tmp_path: Path, bad: str) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)

    with pytest.raises(SemanticLayerError):
        _BearerTokenGate(server.streamable_http_app, bad)  # type: ignore[arg-type]
    with pytest.raises(SemanticLayerError):
        architect_http_app(server, "streamable-http", bad)


# -- authentication ----------------------------------------------------------------


def test_request_without_a_token_is_rejected(tmp_path: Path, token: str) -> None:
    with _architect(tmp_path, token) as (client, _):
        response = _initialize(client)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["message"] == "Missing or invalid bearer token."


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "DELETE", "PUT", "PATCH"])
@pytest.mark.parametrize("path", ["/mcp", "/", "/anything"])
def test_every_method_and_path_needs_the_token(
    tmp_path: Path, token: str, method: str, path: str
) -> None:
    """Including the CORS preflight a browser would send first."""
    with _architect(tmp_path, token) as (client, _):
        response = client.request(method, path, headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer wrong-token",
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "",
        "Bearer {token}x",
        "Bearer {token_prefix}",
        "Bearer {token_upper}",
        "Token {token}",
    ],
    ids=["wrong", "basic", "empty-bearer", "blank", "suffix", "prefix", "case", "scheme"],
)
def test_request_with_a_bad_token_is_rejected(
    tmp_path: Path, token: str, authorization: str
) -> None:
    value = authorization.format(token=token, token_prefix=token[:-1], token_upper=token.upper())
    with _architect(tmp_path, token) as (client, _):
        response = _initialize(client, Authorization=value)

    assert response.status_code == 401


def test_more_than_one_authorization_header_is_rejected(tmp_path: Path, token: str) -> None:
    with _architect(tmp_path, token) as (client, _):
        response = client.post(
            "/mcp",
            json=INITIALIZE,
            headers=[
                *MCP_HEADERS.items(),
                ("Authorization", "Bearer wrong-token"),
                ("Authorization", f"Bearer {token}"),
            ],
        )

    assert response.status_code == 401


def test_request_with_the_token_initializes(tmp_path: Path, token: str) -> None:
    with _architect(tmp_path, token) as (client, _):
        response = _initialize(client, Authorization=f"bearer {token}")

    assert response.status_code == 200
    assert "Semantic Rails Architect MCP" in response.text


def test_sse_endpoints_require_the_token(tmp_path: Path, token: str) -> None:
    with _architect(tmp_path, token, transport="sse") as (client, _):
        stream = client.get("/sse")
        message = client.post("/messages/?session_id=0", json=INITIALIZE)

    assert stream.status_code == 401
    assert message.status_code == 401


# -- Host and Origin checks (DNS-rebinding protection) ------------------------------


def test_authenticated_request_with_a_foreign_host_is_rejected(tmp_path: Path, token: str) -> None:
    with _architect(tmp_path, token) as (client, _):
        response = _initialize(client, Host="attacker.example:8010", **_bearer(token))

    assert response.status_code == 421


def test_authenticated_cross_origin_request_is_rejected(tmp_path: Path, token: str) -> None:
    with _architect(tmp_path, token) as (client, _):
        foreign = _initialize(client, Origin="http://attacker.example", **_bearer(token))
        null_origin = _initialize(client, Origin="null", **_bearer(token))
        same_host = _initialize(client, Origin="http://localhost:8010", **_bearer(token))

    assert foreign.status_code == 403
    assert null_origin.status_code == 403
    assert same_host.status_code == 200


def test_host_and_origin_without_a_port_are_accepted(tmp_path: Path, token: str) -> None:
    """A client on the default HTTP port sends ``Host: name`` with no port."""
    with _architect(tmp_path, token, host="192.0.2.10") as (client, _):
        responses = [
            _initialize(client, Host="192.0.2.10", **_bearer(token)),
            _initialize(client, Host="192.0.2.10:80", **_bearer(token)),
            _initialize(client, Host="localhost", Origin="http://localhost", **_bearer(token)),
            _initialize(client, Host="192.0.2.10:80", Origin="http://192.0.2.10", **_bearer(token)),
        ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]


def test_wildcard_bind_still_checks_host_and_token(tmp_path: Path, token: str) -> None:
    with _architect(tmp_path, token, host="0.0.0.0") as (client, _):
        unauthenticated = _initialize(client)
        foreign_host = _initialize(client, Host="10.0.0.5:8010", **_bearer(token))
        forwarded = _initialize(client, Host="localhost:8010", **_bearer(token))

    assert unauthenticated.status_code == 401
    assert foreign_host.status_code == 421
    assert forwarded.status_code == 200


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _ipv6_loopback_available(), reason="no IPv6 loopback")
def test_an_ipv6_wildcard_server_is_reachable_at_its_advertised_url(
    tmp_path: Path, token: str
) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path, host="::")
    _, config = asyncio.run(
        server.call_tool("mcp_client_config", {"transport": "streamable-http", "host": "::"})
    )
    app = architect_http_app(server, "streamable-http", token)

    with _serve(app, family=socket.AF_INET6) as base_url:
        advertised = config["http"]["url"].replace(":8010", ":" + base_url.rsplit(":", 1)[1])
        with httpx.Client(timeout=10) as client:
            response = client.post(
                advertised, json=INITIALIZE, headers={**MCP_HEADERS, **_bearer(token)}
            )

    assert advertised.startswith("http://[::1]:")
    assert response.status_code == 200


_LOOPBACK = ["127.0.0.1", "localhost", "[::1]"]


@pytest.mark.parametrize(
    ("host", "extra"),
    [
        ("127.0.0.1", []),
        ("LOCALHOST", []),
        ("::1", []),
        ("0.0.0.0", []),
        ("0", []),
        ("::", []),
        ("::0", []),
        ("127.0.0.2", ["127.0.0.2"]),
        ("192.168.1.20", ["192.168.1.20"]),
        ("fe80::1", ["[fe80::1]"]),
    ],
)
def test_host_allowlist_follows_the_bind_address(
    tmp_path: Path, host: str, extra: list[str]
) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path, host=host)
    security = server.settings.transport_security
    names = [*_LOOPBACK, *extra]

    assert security is not None and security.enable_dns_rebinding_protection
    assert security.allowed_hosts == [*names, *(f"{name}:*" for name in names)]
    assert security.allowed_origins == [
        *(f"http://{name}" for name in names),
        *(f"http://{name}:*" for name in names),
    ]


# -- tokens ---------------------------------------------------------------------------


def test_token_sources_and_precedence(monkeypatch, tmp_path: Path) -> None:
    from_env = "e" * 32
    from_env_file = tmp_path / "env.token"
    from_env_file.write_text("f" * 32 + "\n", encoding="utf-8")
    from_flag = tmp_path / "flag.token"
    from_flag.write_text("g" * 32, encoding="utf-8")

    assert load_architect_token() == ""
    monkeypatch.setenv(ARCHITECT_TOKEN_ENV, from_env)
    assert load_architect_token() == from_env
    monkeypatch.setenv(ARCHITECT_TOKEN_FILE_ENV, str(from_env_file))
    assert load_architect_token() == "f" * 32
    assert load_architect_token(str(from_flag)) == "g" * 32


def test_token_file_path_expands_the_home_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "architect.token").write_text("h" * 40, encoding="utf-8")

    assert load_architect_token("~/architect.token") == "h" * 40


def test_bad_tokens_are_rejected_without_echoing_them(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ARCHITECT_TOKEN_ENV, "short-secret")
    with pytest.raises(SemanticLayerError, match="at least 32 characters") as excinfo:
        load_architect_token()
    assert "short-secret" not in str(excinfo.value)

    spaced = "a" * 20 + " " + "b" * 20
    monkeypatch.setenv(ARCHITECT_TOKEN_ENV, spaced)
    with pytest.raises(SemanticLayerError, match="may contain only") as excinfo:
        load_architect_token()
    assert spaced not in str(excinfo.value)


def test_unreadable_empty_or_binary_token_files_are_reported(tmp_path: Path) -> None:
    empty = tmp_path / "empty.token"
    empty.write_text("  \n", encoding="utf-8")
    binary = tmp_path / "binary.token"
    binary.write_bytes(b"\xff\xfe" + b"x" * 40)

    with pytest.raises(SemanticLayerError, match="cannot read the Architect MCP token file"):
        load_architect_token(str(tmp_path / "missing.token"))
    with pytest.raises(SemanticLayerError, match="is empty"):
        load_architect_token(str(empty))
    with pytest.raises(SemanticLayerError, match="is not UTF-8 text"):
        load_architect_token(str(binary))


def test_client_config_names_the_token_but_never_carries_it(
    monkeypatch, tmp_path: Path, token: str
) -> None:
    monkeypatch.setenv(ARCHITECT_TOKEN_ENV, token)
    server = create_architect_mcp_server(workspace_root=tmp_path)

    _, result = asyncio.run(server.call_tool("mcp_client_config", {"transport": "streamable-http"}))

    assert result["http"]["headers"] == {"Authorization": f"Bearer ${{{ARCHITECT_TOKEN_ENV}}}"}
    assert result["sse"]["headers"] == result["http"]["headers"]
    assert result["auth"]["token_env"] == ARCHITECT_TOKEN_ENV
    assert token not in json.dumps(result)


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8010/mcp"),
        ("::", "http://[::1]:8010/mcp"),
        ("::0", "http://[::1]:8010/mcp"),
        ("::1", "http://[::1]:8010/mcp"),
        ("192.168.1.20", "http://192.168.1.20:8010/mcp"),
    ],
)
def test_client_config_urls_are_dialable(tmp_path: Path, host: str, url: str) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)

    _, result = asyncio.run(
        server.call_tool("mcp_client_config", {"transport": "streamable-http", "host": host})
    )

    assert result["http"]["url"] == url


# -- a full MCP client session -----------------------------------------------------


async def _tools_over(transport: str, url: str, headers: dict[str, str]) -> set[str]:
    if transport == "streamable-http":
        http_client = create_mcp_http_client(headers=headers)
        async with (
            http_client,
            streamable_http_client(url, http_client=http_client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            return {tool.name for tool in (await session.list_tools()).tools}
    async with (
        sse_client(url, headers=headers) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        return {tool.name for tool in (await session.list_tools()).tools}


@pytest.mark.parametrize(("transport", "path"), [("streamable-http", "/mcp"), ("sse", "/sse")])
def test_mcp_client_session_over_the_network_transport(
    tmp_path: Path, token: str, transport: str, path: str
) -> None:
    with _architect(tmp_path, token, transport=transport) as (_, base_url):
        tools = asyncio.run(_tools_over(transport, base_url + path, _bearer(token)))
        assert "write_project_file" in tools
        with pytest.raises(ExceptionGroup) as excinfo:
            asyncio.run(_tools_over(transport, base_url + path, {}))

    assert excinfo.group_contains(httpx.HTTPStatusError, depth=1)
    rejected = [
        error for error in excinfo.value.exceptions if isinstance(error, httpx.HTTPStatusError)
    ]
    assert rejected and rejected[0].response.status_code == 401
