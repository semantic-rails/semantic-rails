"""MCP JSON-RPC dispatcher and stdio transport.

Streamable HTTP shares the dispatcher through :mod:`semantic_rails.mcp_streamable_http`.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping

# Use ThreadingHTTPServer so concurrent MCP HTTP calls (discover, inspect,
# build-options) do not serialize. Runtime caches are thread-safe.
from typing import Any, Protocol, TextIO

from .audit import emit_audit_event
from .diagnostics import recovery_hints_for_error
from .errors import UNEXPECTED_ERROR_MESSAGE, SemanticLayerError
from .mcp import MCP_SERVER_INSTRUCTIONS, SemanticLayerMCPAdapter, json_text
from .mcp_session import MCPQuerySession
from .request_context import (
    RequestContext,
    request_context_payload,
)

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS = (
    MCP_PROTOCOL_VERSION,
    "2025-03-26",
    "2024-11-05",
)


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


def _tool_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json_text(payload)}],
        "structuredContent": payload,
        "isError": not bool(payload.get("ok", True)),
    }


class MCPAdapter(Protocol):
    """What :func:`handle_jsonrpc_message` calls on its adapter.

    :class:`SemanticLayerMCPAdapter` is one; a host may pass its own. Optional
    ``interface`` and ``instructions`` attributes fill the ``initialize`` result
    (``"v2"`` and the engine's instructions when absent).
    """

    @property
    def package_id(self) -> str: ...

    def list_tools(self) -> list[dict[str, Any]]: ...

    def call_tool(
        self, name: str, arguments: dict[str, Any], /, *, request_context: RequestContext | None
    ) -> dict[str, Any]: ...

    def list_resources(self) -> list[dict[str, Any]]: ...

    def read_resource(
        self, uri: str, /, *, request_context: RequestContext | None
    ) -> dict[str, Any]: ...

    def list_prompts(self) -> list[dict[str, Any]]: ...

    def get_prompt(self, name: str, arguments: dict[str, Any], /) -> dict[str, Any]: ...


def handle_jsonrpc_message(
    adapter: MCPAdapter,
    message: dict[str, Any],
    *,
    request_context: RequestContext | None = None,
    session: MCPQuerySession | None = None,
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
                # An adapter-shaped object without these attributes serves v2.
                "serverInfo": {
                    "name": "semantic-rails",
                    "version": getattr(adapter, "interface", "v2"),
                },
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "instructions": getattr(adapter, "instructions", MCP_SERVER_INSTRUCTIONS),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": adapter.list_tools()}
        elif method == "tools/call":
            raw_arguments = params.get("arguments", {}) or {}
            if not isinstance(raw_arguments, Mapping):
                return _jsonrpc_error(message_id, -32602, "MCP tool arguments must be an object")
            if session is not None and isinstance(adapter, SemanticLayerMCPAdapter):
                payload = adapter.call_tool(
                    str(params.get("name", "")),
                    dict(raw_arguments),
                    request_context=request_context,
                    session=session,
                )
            else:
                payload = adapter.call_tool(
                    str(params.get("name", "")),
                    dict(raw_arguments),
                    request_context=request_context,
                )
            result = _tool_content(payload)
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
            UNEXPECTED_ERROR_MESSAGE,
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
    session = MCPQuerySession()
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
                response = handle_jsonrpc_message(adapter, message, session=session)
        if response is not None:
            output_stream.write(json.dumps(response, sort_keys=True, default=str) + "\n")
            output_stream.flush()


def refuse_stdio(
    error: SemanticLayerError,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> None:
    """Answer every stdio request with ``error``, for a server that can't start.

    stdout is the protocol channel, so a client shows an error printed there
    as a closed connection. This logs it to stderr and returns it as the
    JSON-RPC error of each request (``initialize`` first) until the client
    disconnects.
    """

    print(f"semantic-rails mcp stdio: {error}", file=sys.stderr, flush=True)
    output_stream = output_stream or sys.stdout
    for line in input_stream or sys.stdin:
        try:
            message_id = json.loads(line).get("id")
        except (AttributeError, json.JSONDecodeError):
            continue
        if message_id is not None:
            data: dict[str, Any] = {"code": error.code}
            if error.details:
                data["details"] = dict(error.details)
            reply = _jsonrpc_error(message_id, -32603, str(error), data=data)
            output_stream.write(json.dumps(reply, sort_keys=True, default=str) + "\n")
            output_stream.flush()
