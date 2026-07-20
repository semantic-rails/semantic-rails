"""Error envelope audit — every advertised path must produce a
structured envelope, never a bare leak.

Two contracts:

1. Every error code in ``ERROR_CODES`` that is reachable from the MCP
   boundary surfaces an envelope with ``code``, ``message`` and at
   least one of ``details`` / ``recovery_hints`` / ``closest_matches``.
2. A bare ``KeyError`` / ``AttributeError`` thrown deep in a handler
   surfaces as a structured ``INTERNAL_ERROR`` envelope with a public
   ``recovery_hints`` entry pointing at the bug tracker — never a raw
   traceback.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails.errors import ERROR_CODES
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import handle_jsonrpc_message


def _assert_envelope_shape(error: dict[str, Any]) -> None:
    """Every advertised error envelope must carry these keys, with at
    least one of details / recovery_hints / closest_matches populated."""
    assert isinstance(error, dict), f"error must be a dict, got {type(error).__name__}"
    assert error.get("code"), "error envelope must have a 'code'"
    assert error.get("message"), "error envelope must have a 'message'"
    details = error.get("details", {}) or {}
    hints = error.get("recovery_hints", []) or []
    closest = list(details.get("closest_matches", []) or []) + list(
        error.get("closest_matches", []) or []
    )
    assert details or hints or closest, (
        f"error envelope for code={error.get('code')!r} must carry at least "
        f"one of details/recovery_hints/closest_matches"
    )


# ---------------------------------------------------------------------
# Triggers — code -> (adapter call kwargs) producer
# ---------------------------------------------------------------------


def _trigger_object_not_found(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    return adapter.call_tool("inspect", {"object_id": "measure.jaffle.nonexistent_xyz"})


def _trigger_invalid_mcp_arguments(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    return adapter.call_tool("compile", {"query": "this is not a query"})


def _trigger_unknown_mcp_tool(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    return adapter.call_tool("hyperdrive", {})


def _trigger_unknown_mcp_prompt(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    # get_prompt returns a structured envelope directly (not raise) when
    # the prompt is unknown. Normalize into the audit's expected shape.
    result = adapter.get_prompt("nonexistent_prompt", {})
    if "recovery_hints" not in result:
        # Errors list is populated from `error.recovery_hints`.
        error = dict(result.get("error", {}) or {})
        result["recovery_hints"] = list(error.get("recovery_hints", []) or [])
    return result


def _trigger_unknown_mcp_resource(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    # read_resource returns a resource-shaped envelope; the structured
    # payload lives in the JSON-serialized `text` field. Unwrap to the
    # shape the audit expects.
    import json

    raw = adapter.read_resource("not://a/real/uri")
    text = str(raw.get("text", "") or "")
    if not text:
        return {"ok": False, "errors": [], "recovery_hints": []}
    parsed = dict(json.loads(text) or {})
    if "recovery_hints" not in parsed:
        error = dict(parsed.get("error", {}) or {})
        parsed["recovery_hints"] = list(error.get("recovery_hints", []) or [])
    return parsed


# Map advertised error codes to the trigger that produces them. Codes
# not in this map are exercised by their dedicated test suites — what
# matters here is that *every* MCP-advertised raise produces a
# structured envelope. The exhaustive code -> raise-site mapping is
# already enforced by `test_error_codes_contract.py`.
_MCP_TRIGGERED_CODES = {
    "OBJECT_NOT_FOUND": _trigger_object_not_found,
    "INVALID_MCP_ARGUMENTS": _trigger_invalid_mcp_arguments,
    "UNKNOWN_MCP_TOOL": _trigger_unknown_mcp_tool,
    "UNKNOWN_MCP_PROMPT": _trigger_unknown_mcp_prompt,
    "UNKNOWN_MCP_RESOURCE": _trigger_unknown_mcp_resource,
}


@pytest.mark.parametrize("code", sorted(_MCP_TRIGGERED_CODES.keys()))
def test_mcp_advertised_error_codes_produce_structured_envelopes(runtime_factory, code) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        result = _MCP_TRIGGERED_CODES[code](adapter)
        assert result is not None, f"trigger for {code} produced None"
        assert result.get("ok") is False, f"trigger for {code} returned ok=True"
        errors = list(result.get("errors", []) or [])
        assert errors, f"trigger for {code} returned no errors"
        # The first error should match the expected code (or a related
        # boundary code — the trigger for OBJECT_NOT_FOUND can surface
        # as a different code if the registry shape changes).
        first = errors[0]
        _assert_envelope_shape(first)
        if code in {"OBJECT_NOT_FOUND", "INVALID_MCP_ARGUMENTS", "UNKNOWN_MCP_TOOL"}:
            assert first["code"] == code, (
                f"expected code={code}, got {first['code']}: {first.get('message')}"
            )
    finally:
        runtime.close()


def test_mcp_handler_bare_exception_surfaces_as_internal_error(runtime_factory) -> None:
    """A bare KeyError thrown inside a handler must be wrapped in a
    structured INTERNAL_ERROR envelope, not surfaced as a raw
    traceback."""
    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)

        # Replace the handler with one that routes through `_guarded`
        # — the safety net we are testing — and raises a bare
        # KeyError inside the inner closure. Bypassing `_guarded` would
        # also bypass the bare-exception trap, which is not what
        # production callers see.
        def _exploding_handler(arguments):
            def _inner(args):
                raise KeyError("missing_field")

            return adapter._guarded(arguments, _inner)  # noqa: SLF001

        adapter._tool_handlers["catalog"] = _exploding_handler  # noqa: SLF001
        result = adapter.call_tool("catalog", {})
        assert result["ok"] is False
        errors = list(result.get("errors", []) or [])
        assert errors
        first = errors[0]
        assert first["code"] == "INTERNAL_ERROR"
        assert "KeyError" in first["message"]
        hints = list(result.get("recovery_hints", []) or [])
        assert hints, "INTERNAL_ERROR envelope must carry recovery_hints"
        # Hint must point at the bug tracker so the surface is at
        # least actionable.
        joined = " ".join(str(h.get("message", "")) for h in hints).lower()
        assert "bug" in joined or "issues" in joined
    finally:
        runtime.close()


def test_mcp_jsonrpc_bare_exception_surfaces_as_structured_envelope(runtime_factory) -> None:
    """The JSON-RPC bare-exception path must wrap leaks in a structured
    `data` envelope. Before this fix it shipped a raw
    `MCP error -32603: '<repr>'` with no recovery hints."""
    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)

        # Patch a tool to raise a bare exception mid-flight. The
        # adapter wraps it as INTERNAL_ERROR; if that envelope
        # were missing, the JSON-RPC path would be the one shipping
        # the bare leak.
        def _exploding_handler(arguments):
            def _inner(args):
                raise AttributeError("nope")

            return adapter._guarded(arguments, _inner)  # noqa: SLF001

        adapter._tool_handlers["discover"] = _exploding_handler  # noqa: SLF001
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "discover", "arguments": {"terms": "revenue"}},
        }
        response = handle_jsonrpc_message(adapter, message)
        assert response is not None
        # The adapter returns a successful result wrapping a tool
        # output whose `isError` flag is True. Inspect the structured
        # content.
        result = response.get("result")
        assert isinstance(result, dict)
        structured = dict(result.get("structuredContent", {}) or {})
        assert structured.get("ok") is False
        errors = list(structured.get("errors", []) or [])
        assert errors
        first = errors[0]
        assert first["code"] == "INTERNAL_ERROR"
        hints = list(structured.get("recovery_hints", []) or [])
        assert hints
    finally:
        runtime.close()


def test_http_bare_exception_surfaces_as_structured_internal_error(runtime_factory) -> None:
    """HTTP boundary's `exception_payload` must wrap bare exceptions
    in a structured INTERNAL_ERROR envelope."""
    runtime = runtime_factory("jaffle_shop")
    try:
        service = SemanticHTTPService(runtime)
        result, status = service.exception_payload(KeyError("missing_column"), stage="http")
        assert status == 500
        error = dict(result.get("error", {}) or {})
        assert error.get("code") == "INTERNAL_ERROR"
        assert "KeyError" in error.get("message", "")
        hints = list(result.get("recovery_hints", []) or [])
        assert hints, "HTTP INTERNAL_ERROR must carry recovery_hints"
        joined = " ".join(str(h.get("message", "")) for h in hints).lower()
        assert "bug" in joined or "issues" in joined
        details = dict(error.get("details", {}) or {})
        assert details.get("exception_type") == "KeyError"
    finally:
        runtime.close()


def test_error_codes_module_is_self_consistent() -> None:
    """Sanity check — ERROR_CODES is a non-empty set of strings. Codes
    we add to envelopes (like INTERNAL_ERROR) that are not raised via
    SemanticLayerError do not need to be in this set, but the set
    itself must be coherent."""
    assert isinstance(ERROR_CODES, set)
    assert ERROR_CODES
    assert all(isinstance(code, str) and code for code in ERROR_CODES)
