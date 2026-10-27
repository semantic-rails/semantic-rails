"""Inspect with a typo'd ``object_id`` must surface ``closest_matches``.

Reviewer flagged that asking ``inspect("measure.jaffle.order_kount")``
returned a bare error with no suggestion. The MCP and HTTP boundaries
now emit a structured ``OBJECT_NOT_FOUND`` envelope with a
``closest_matches`` list (top-3 by edit distance) so callers can
recover without rebrowsing the whole catalog.
"""

from __future__ import annotations

import pytest

from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import RequestContext


@pytest.fixture()
def runtime(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


def test_mcp_inspect_typo_returns_closest_matches(runtime) -> None:
    adapter = SemanticLayerMCPAdapter(runtime)
    result = adapter.call_tool(
        "inspect",
        {"object_id": "measure.jaffle.order_kount"},
    )
    assert result["ok"] is False
    errors = list(result.get("errors", []) or [])
    assert errors, "expected a structured error"
    first = errors[0]
    assert first["code"] == "OBJECT_NOT_FOUND"
    details = dict(first.get("details", {}) or {})
    closest = list(details.get("closest_matches", []) or [])
    assert closest, "expected at least one closest match"
    assert "measure.jaffle.order_count" in closest
    # Hints must include the same suggestions so callers reading
    # `recovery_hints` alone can recover.
    hints = list(result["errors"][0].get("recovery_hints", []) or [])
    assert hints, "expected non-empty recovery_hints"
    hint_closest = list(hints[0].get("closest_matches", []) or [])
    assert "measure.jaffle.order_count" in hint_closest


def test_http_inspect_typo_returns_closest_matches(runtime) -> None:
    service = SemanticHTTPService(runtime)
    # `/inspect` raises through `exception_payload`, which then
    # builds the structured envelope (status 400 for known
    # `SemanticLayerError` codes).
    try:
        service.handle(
            "POST",
            "/inspect",
            {"object_id": "measure.jaffle.order_kount"},
        )
        raise AssertionError("expected SemanticLayerError to propagate")
    except Exception as exc:  # noqa: BLE001 — assertion harness
        result, status = service.exception_payload(exc, stage="http", context=RequestContext())
    assert status == 400
    error = dict(result.get("error", {}) or {})
    assert error.get("code") == "OBJECT_NOT_FOUND"
    details = dict(error.get("details", {}) or {})
    closest = list(details.get("closest_matches", []) or [])
    assert closest
    assert "measure.jaffle.order_count" in closest
