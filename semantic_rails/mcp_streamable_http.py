"""Stateless MCP Streamable HTTP request handling.

This module contains no server framework dependencies. The ASGI deployment
uses it at ``/mcp`` while the existing stdio and legacy HTTP/SSE transports
remain available for local compatibility.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .http_core import MAX_REQUEST_BODY_BYTES, cors_origin_header, request_id_from_parts
from .mcp import SemanticLayerMCPAdapter
from .mcp_server import (
    MCP_PROTOCOL_VERSION,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
    _jsonrpc_error,
    handle_jsonrpc_message,
)
from .request_context import RequestContext, get_policy_context_resolver

MCP_MAX_REQUEST_BYTES = MAX_REQUEST_BODY_BYTES
MCP_ACCEPT_JSON = "application/json"
MCP_ACCEPT_SSE = "text/event-stream"


@dataclass(frozen=True)
class MCPHTTPResponse:
    status: int
    payload: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _header(headers: Mapping[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _error(status: int, code: int, message: str) -> MCPHTTPResponse:
    return MCPHTTPResponse(
        status=status,
        payload=_jsonrpc_error(None, code, message),
        headers={
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Vary": "Origin",
        },
    )


def _valid_accept(value: str) -> bool:
    media_types = {
        part.split(";", 1)[0].strip().lower() for part in value.split(",") if part.strip()
    }
    return MCP_ACCEPT_JSON in media_types and MCP_ACCEPT_SSE in media_types


def handle_streamable_http_request(
    adapter: SemanticLayerMCPAdapter,
    *,
    method: str,
    headers: Mapping[str, Any],
    body: bytes = b"",
    request_context: RequestContext | None = None,
) -> MCPHTTPResponse:
    method = method.upper()
    origin = _header(headers, "Origin")
    # MCP Streamable HTTP requires Origin validation to block DNS-rebinding
    # and drive-by access from a page in the operator's own browser. With no
    # allow-list configured `cors_origin_header` returns "", so every
    # Origin-bearing (i.e. browser) request is rejected and non-browser
    # clients — which send no Origin — are unaffected.
    if origin and not cors_origin_header(origin):
        return _error(403, -32003, "Origin is not allowed for this MCP endpoint.")

    common_headers = {
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Vary": "Origin",
    }
    if method == "OPTIONS":
        return MCPHTTPResponse(status=204, headers=common_headers)
    if method == "GET":
        return MCPHTTPResponse(
            status=405,
            payload=_jsonrpc_error(
                None, -32600, "This stateless MCP endpoint accepts POST requests only."
            ),
            headers={**common_headers, "Allow": "POST, OPTIONS"},
        )
    if method != "POST":
        return MCPHTTPResponse(
            status=405,
            payload=_jsonrpc_error(None, -32600, f"Unsupported HTTP method '{method}'."),
            headers={**common_headers, "Allow": "POST, OPTIONS"},
        )

    content_type = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
    if content_type != MCP_ACCEPT_JSON:
        return _error(415, -32600, "MCP requests must use Content-Type: application/json.")
    if not _valid_accept(_header(headers, "Accept")):
        return _error(
            406,
            -32600,
            "MCP requests must accept both application/json and text/event-stream.",
        )
    if len(body) > MCP_MAX_REQUEST_BYTES:
        return _error(413, -32600, "MCP request body exceeds the 64 KiB limit.")

    protocol_header = _header(headers, "MCP-Protocol-Version")
    if protocol_header and protocol_header not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
        return _error(
            400,
            -32600,
            f"Unsupported MCP-Protocol-Version '{protocol_header}'.",
        )

    try:
        decoded = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _error(400, -32700, f"Invalid JSON: {exc}")
    if not isinstance(decoded, dict):
        return _error(400, -32600, "Streamable HTTP accepts one JSON-RPC object per request.")

    message = dict(decoded)
    # Streamable HTTP is always a remote boundary. Resolve identity only
    # from transport-trusted inputs; the JSON-RPC body may contain spoofed
    # outer or nested policy_context values and is deliberately excluded.
    if request_context is None:
        request_context = get_policy_context_resolver().resolve(
            headers,
            payload=None,
            request_id=request_id_from_parts(headers),
        )
    response = handle_jsonrpc_message(adapter, message, request_context=request_context)
    if response is None:
        return MCPHTTPResponse(status=202, headers=common_headers)
    return MCPHTTPResponse(status=200, payload=response, headers=common_headers)
