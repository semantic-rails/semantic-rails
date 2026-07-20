"""MCP transports — stdio JSON-RPC loop and ThreadingHTTPServer.

Two entry points: :func:`serve_stdio` (the canonical MCP transport an
LLM host launches as a subprocess) and :func:`serve_http` (HTTP wrapper
for in-network agents). Both wrap a :class:`SemanticLayerMCPAdapter`
from :mod:`semantic_rails.mcp` and translate the JSON-RPC envelope —
``tools/list``, ``tools/call``, ``resources/list``, ``resources/read``,
``prompts/list``, ``prompts/get``.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Iterable, Mapping

# Use ThreadingHTTPServer so concurrent MCP HTTP calls (discover, inspect,
# build-options) do not serialize. Runtime caches are thread-safe.
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as HTTPServer
from typing import Any, TextIO, cast
from urllib.parse import parse_qs, urlparse

from .diagnostics import recovery_hints_for_error
from .errors import SemanticLayerError
from .http_core import (
    CORS_ALLOW_HEADERS,
    MAX_REQUEST_BODY_BYTES,
    clean_request_id,
    cors_origin_header,
)
from .mcp import SemanticLayerMCPAdapter
from .request_context import (
    RequestContext,
    api_key_auth_result,
    emit_audit_event,
    get_policy_context_resolver,
    request_context_payload,
    warn_if_default_policy_resolver_exposed,
)

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS = (
    MCP_PROTOCOL_VERSION,
    "2025-03-26",
    "2024-11-05",
)
MCP_CORS_ALLOW_METHODS = "GET, POST, OPTIONS"


def _jsonrpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}


def _jsonrpc_error(
    message_id: Any, code: int, message: str, *, data: Any | None = None
) -> dict[str, Any]:
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": message_id,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        payload["error"]["data"] = data
    return payload


def _route(handler: BaseHTTPRequestHandler) -> str:
    route = urlparse(str(getattr(handler, "path", "/") or "/")).path.rstrip("/")
    return route or "/"


def _query_params(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    parsed = parse_qs(urlparse(str(getattr(handler, "path", "") or "")).query)
    return {key: values[-1] for key, values in parsed.items() if values}


def _request_id(handler: BaseHTTPRequestHandler) -> str:
    existing = str(getattr(handler, "_semantic_request_id", "") or "")
    if existing:
        return existing
    request_id = (
        clean_request_id(handler.headers.get("X-Request-ID", ""))
        or clean_request_id(handler.headers.get("X-Correlation-ID", ""))
        or clean_request_id(_query_params(handler).get("request_id", ""))
        or uuid.uuid4().hex
    )
    handler._semantic_request_id = request_id  # type: ignore[attr-defined]
    return request_id


def _send_transport_headers(
    handler: BaseHTTPRequestHandler, *, request_id: str | None = None
) -> None:
    allow_origin = cors_origin_header(handler.headers.get("Origin"))
    if allow_origin:
        handler.send_header("Access-Control-Allow-Origin", allow_origin)
    handler.send_header("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS)
    handler.send_header("Access-Control-Expose-Headers", "X-Request-ID")
    handler.send_header("Access-Control-Allow-Methods", MCP_CORS_ALLOW_METHODS)
    request_id = clean_request_id(request_id or _request_id(handler))
    if request_id:
        handler.send_header("X-Request-ID", request_id)


def _tool_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True, default=str)}
        ],
        "structuredContent": payload,
        "isError": not bool(payload.get("ok", True)),
    }


def handle_jsonrpc_message(
    adapter: SemanticLayerMCPAdapter,
    message: dict[str, Any],
    *,
    request_context: RequestContext | None = None,
) -> dict[str, Any] | None:
    message_id = message.get("id")
    is_notification = "id" not in message
    if message.get("jsonrpc") not in (None, JSONRPC_VERSION):
        return (
            None
            if is_notification
            else _jsonrpc_error(message_id, -32600, "JSON-RPC message must use version '2.0'")
        )
    raw_method = message.get("method")
    method = raw_method if isinstance(raw_method, str) else ""
    if not method:
        return (
            None
            if is_notification
            else _jsonrpc_error(message_id, -32600, "JSON-RPC message must include a method")
        )
    if is_notification or method.startswith("notifications/"):
        return None
    raw_params = message.get("params", {}) or {}
    if not isinstance(raw_params, Mapping):
        return _jsonrpc_error(message_id, -32602, "JSON-RPC params must be an object")
    params = dict(raw_params)
    started = time.perf_counter()
    try:
        if method == "initialize":
            requested_version = str(params.get("protocolVersion", "") or "")
            negotiated_version = (
                requested_version
                if requested_version in MCP_SUPPORTED_PROTOCOL_VERSIONS
                else MCP_PROTOCOL_VERSION
            )
            result: dict[str, Any] = {
                "protocolVersion": negotiated_version,
                "serverInfo": {"name": "semantic-rails", "version": "v1"},
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": adapter.list_tools()}
        elif method == "tools/call":
            raw_arguments = params.get("arguments", {}) or {}
            if not isinstance(raw_arguments, Mapping):
                return _jsonrpc_error(message_id, -32602, "MCP tool arguments must be an object")
            result = _tool_content(
                adapter.call_tool(
                    str(params.get("name", "")),
                    dict(raw_arguments),
                    request_context=request_context,
                )
            )
        elif method == "resources/list":
            result = {"resources": adapter.list_resources()}
        elif method == "resources/read":
            resource = adapter.read_resource(
                str(params.get("uri", "")), request_context=request_context
            )
            result = {
                "contents": [
                    {
                        "uri": resource["uri"],
                        "mimeType": resource["mimeType"],
                        "text": resource["text"],
                    }
                ]
            }
        elif method == "prompts/list":
            result = {"prompts": adapter.list_prompts()}
        elif method == "prompts/get":
            raw_arguments = params.get("arguments", {}) or {}
            if not isinstance(raw_arguments, Mapping):
                return _jsonrpc_error(message_id, -32602, "MCP prompt arguments must be an object")
            result = adapter.get_prompt(str(params.get("name", "")), dict(raw_arguments))
        else:
            return _jsonrpc_error(message_id, -32601, f"Unknown MCP method '{method}'")
        emit_audit_event(
            "mcp_jsonrpc",
            method=method,
            package_id=adapter.package_id,
            request_id=request_context.request_id if request_context is not None else "",
            request_context=request_context_payload(request_context),
            status="ok",
            timing_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return _jsonrpc_result(message_id, result)
    except SemanticLayerError as exc:
        # Enrich the JSON-RPC error envelope with the same recovery hints
        # the structured-tool-result path produces. Without this, an
        # `INVALID_MCP_ARGUMENTS` raised inside `_policy_context_payload`
        # (or any other pre-dispatch validation) escapes as a
        # `SemanticLayerError`, is caught here, and ships back with empty
        # `recovery_hints` — even though `diagnostics.py` already knows the
        # right hint for the offending field. The top-level
        # `closest_valid_query` is also populated from the first hint that
        # carries one, so agents reading the documented envelope find the
        # IR template where the docs promised it lives.
        hints = recovery_hints_for_error(exc.code, exc.details)
        data: dict[str, Any] = {
            "code": exc.code,
            "details": dict(exc.details or {}),
            "recovery_hints": hints,
        }
        for hint in hints:
            template = (
                dict(hint.get("closest_valid_query", {}) or {}) if isinstance(hint, dict) else {}
            )
            if template:
                data["closest_valid_query"] = template
                break
        data.setdefault("closest_valid_query", {})
        return _jsonrpc_error(message_id, -32000, str(exc), data=data)
    except Exception as exc:  # pragma: no cover - defensive server boundary
        # Wrap bare exceptions in a structured envelope. Without `data`,
        # callers see only ``MCP error -32603: 'field'`` and have no way
        # to distinguish a transient bug from a misuse. The recovery
        # hint points at the bug tracker so the surface is at least
        # actionable.
        import logging

        logging.getLogger(__name__).exception(
            "unhandled exception in JSON-RPC handler: %s",
            exc,
        )
        return _jsonrpc_error(
            message_id,
            -32603,
            f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
            data={
                "code": "INTERNAL_ERROR",
                "details": {"exception_type": type(exc).__name__},
                "recovery_hints": [
                    {
                        "kind": "file_bug_report",
                        "message": (
                            "An unexpected error reached the JSON-RPC "
                            "boundary. Retry once; if it recurs, please "
                            "file a bug at "
                            "https://github.com/semantic-rails/semantic-rails/issues "
                            "with the JSON-RPC method, params, and the "
                            "request_id from this response."
                        ),
                    }
                ],
                "closest_valid_query": {},
            },
        )


def serve_stdio(
    adapter: SemanticLayerMCPAdapter,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> None:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for line in input_stream:
        if not line.strip():
            continue
        response: dict[str, Any] | None
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _jsonrpc_error(None, -32700, f"Invalid JSON: {exc.msg}")
        else:
            if not isinstance(message, dict):
                response = _jsonrpc_error(None, -32600, "JSON-RPC message must be an object")
            else:
                response = handle_jsonrpc_message(adapter, message)
        if response is not None:
            output_stream.write(json.dumps(response, sort_keys=True, default=str) + "\n")
            output_stream.flush()


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any] | list[Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError as exc:
        raise ValueError("invalid Content-Length header") from exc
    if length > MAX_REQUEST_BODY_BYTES:
        raise ValueError(f"request body exceeds the {MAX_REQUEST_BODY_BYTES // 1024} KiB limit")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    payload = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(payload, (dict, list)):
        raise ValueError("request body must be a JSON object or array")
    return payload


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    _send_transport_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_empty(handler: BaseHTTPRequestHandler, status: int) -> None:
    handler.send_response(status)
    _send_transport_headers(handler)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _send_sse(handler: BaseHTTPRequestHandler, events: Iterable[tuple[str, Any]]) -> None:
    chunks = []
    for event_name, data in events:
        chunks.append(f"event: {event_name}\n")
        chunks.append(f"data: {json.dumps(data, sort_keys=True, default=str)}\n\n")
    body = "".join(chunks).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    _send_transport_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _discovery_payload(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    """Return the route-discovery banner served on ``GET /`` and embedded in
    error envelopes for unknown HTTP routes. Lets a fresh agent learn the
    JSON-RPC endpoint on its first call instead of seeing a flat 404 — the
    blind UX walkthrough stalled on exactly this surface."""
    from semantic_rails.package_tools import _semantic_rails_version  # local import to avoid cycle

    return {
        "service": "semantic-rails-mcp",
        "protocol": "jsonrpc-2.0",
        "version": _semantic_rails_version(),
        "package_id": adapter.package_id,
        "tools_count": len(adapter.list_tools()),
        "routes": {
            "/mcp": "JSON-RPC 2.0 endpoint (POST)",
            "/sse": "Server-Sent Events transport (GET)",
            "/health": "Health probe (GET)",
        },
    }


def make_mcp_http_handler(adapter: SemanticLayerMCPAdapter) -> type[BaseHTTPRequestHandler]:
    class MCPHttpHandler(BaseHTTPRequestHandler):
        def _auth_ok(self) -> bool:
            if _route(self) == "/health":
                return True
            ok, _ = api_key_auth_result(cast(Mapping[str, Any], self.headers))
            return ok

        def _unauthorized(self) -> None:
            _send_json(
                self, 401, _jsonrpc_error(None, -32001, "Missing or invalid bearer API key.")
            )

        def do_OPTIONS(self) -> None:  # noqa: N802
            return _send_empty(self, 204)

        def _unknown_route_error(self, status: int = 404) -> None:
            """Return a 404 that includes the route-discovery payload, so
            agents that miss the `GET /` banner still learn the JSON-RPC
            endpoint from their first error."""
            envelope = _jsonrpc_error(None, -32601, "Unknown MCP HTTP route.")
            error_block = envelope.setdefault("error", {})
            data_block = error_block.setdefault("data", {})
            data_block.update(_discovery_payload(adapter))
            return _send_json(self, status, envelope)

        def do_GET(self) -> None:  # noqa: N802
            if not self._auth_ok():
                return self._unauthorized()
            route = _route(self)
            if route in ("/", ""):
                # Route discovery banner — first call most agents make.
                return _send_json(self, 200, _discovery_payload(adapter))
            if route == "/health":
                return _send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "semantic-rails-mcp",
                        "package_id": adapter.package_id,
                        "instance_nonce": os.environ.get("SEMANTIC_RAILS_MCP_INSTANCE_NONCE", ""),
                        "version": _discovery_payload(adapter)["version"],
                        "tools_count": len(adapter.list_tools()),
                    },
                )
            if route == "/sse":
                context = get_policy_context_resolver().resolve(
                    cast(Mapping[str, Any], self.headers),
                    payload=None,
                    request_id=_request_id(self),
                )
                return _send_sse(
                    self,
                    [
                        (
                            "endpoint",
                            {
                                "path": "/mcp",
                                "package_id": adapter.package_id,
                                "request_context": context.to_public_dict(),
                            },
                        ),
                        ("ready", {"ok": True, "tools": len(adapter.list_tools())}),
                    ],
                )
            return self._unknown_route_error()

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth_ok():
                return self._unauthorized()
            if _route(self) != "/mcp":
                return self._unknown_route_error()
            # ``response`` is union-typed across this block: a single
            # JSON-RPC response object (dict), a batch (list), or
            # ``None`` for a notification-only batch. The dispatcher
            # below (``if response is None``) branches on the None
            # case explicitly; a future refactor that adds a fourth
            # shape would need to update the dispatcher too.
            response: dict[str, Any] | list[dict[str, Any]] | None
            try:
                context = get_policy_context_resolver().resolve(
                    cast(Mapping[str, Any], self.headers),
                    payload=None,
                    request_id=_request_id(self),
                )
                payload = _read_json(self)
                if isinstance(payload, list):
                    if not payload:
                        response = _jsonrpc_error(
                            None, -32600, "JSON-RPC batch must contain at least one message"
                        )
                    else:
                        batch: list[dict[str, Any]] = []
                        for item in payload:
                            if not isinstance(item, dict):
                                batch.append(
                                    _jsonrpc_error(
                                        None, -32600, "JSON-RPC batch items must be objects"
                                    )
                                )
                                continue
                            row = handle_jsonrpc_message(adapter, item, request_context=context)
                            if row is not None:
                                batch.append(row)
                        # Notification-only batch (no responses needed)
                        # collapses to ``None`` so the dispatcher emits
                        # a 204 No Content.
                        response = batch if batch else None
                else:
                    response = handle_jsonrpc_message(adapter, payload, request_context=context)
            except json.JSONDecodeError as exc:
                return _send_json(
                    self, 400, _jsonrpc_error(None, -32700, f"Invalid JSON: {exc.msg}")
                )
            except ValueError as exc:
                return _send_json(self, 400, _jsonrpc_error(None, -32600, str(exc)))
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                import logging

                logging.getLogger(__name__).exception(
                    "unhandled exception in MCP HTTP boundary: %s",
                    exc,
                )
                return _send_json(
                    self,
                    500,
                    _jsonrpc_error(
                        None,
                        -32603,
                        f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                        data={
                            "code": "INTERNAL_ERROR",
                            "details": {"exception_type": type(exc).__name__},
                            "recovery_hints": [
                                {
                                    "kind": "file_bug_report",
                                    "message": (
                                        "An unexpected error reached the "
                                        "MCP HTTP boundary. Retry once; if "
                                        "it recurs, please file a bug at "
                                        "https://github.com/semantic-rails/semantic-rails/issues."
                                    ),
                                }
                            ],
                        },
                    ),
                )
            if response is None:
                return _send_empty(self, 204)
            return _send_json(self, 200, response)

    return MCPHttpHandler


def serve_http(
    adapter: SemanticLayerMCPAdapter, *, host: str = "127.0.0.1", port: int = 8091
) -> None:
    handler = make_mcp_http_handler(adapter)
    httpd = HTTPServer((host, port), handler)
    warn_if_default_policy_resolver_exposed(host, transport="semantic-rails MCP HTTP")
    print(
        f"semantic-rails MCP HTTP server running on http://{host}:{port}/mcp package={adapter.package_id}"
    )
    httpd.serve_forever()
