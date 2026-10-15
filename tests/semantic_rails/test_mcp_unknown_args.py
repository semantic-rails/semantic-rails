"""Every MCP tool rejects or warns on an unknown argument, naming the closest real one.

Strict tools refuse with ``INVALID_MCP_ARGUMENTS``; the others warn with
``<TOOL>_UNKNOWN_ARG`` and ignore the value.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails.mcp import (
    _STRICT_REJECT_TOOLS,
    _UNKNOWN_ARG_WARNING_CODE,
    _WARN_AND_IGNORE_TOOLS,
    TOOL_DEFINITIONS,
    SemanticLayerMCPAdapter,
)

# Per tool: the smallest valid arguments, then a typo and the argument it should suggest.
_CASES: dict[str, tuple[dict[str, Any], str, Any, str]] = {
    "discover": ({"terms": "revenue"}, "limitt", 5, "limit"),
    "inspect": (
        {"object_id": "measure.jaffle.revenue_usd"},
        "object_ids",
        "measure.jaffle.revenue_usd",
        "object_id",
    ),
    "valid-values": (
        {"dimension_id": "dimension.jaffle.store_name"},
        "dimension",
        "x",
        "dimension_id",
    ),
    "plan": ({"intent": "revenue"}, "intentt", "revenue", "intent"),
    "execute": (
        {"query": {"select": [{"expression": "measure.jaffle.revenue_usd", "as": "r"}]}},
        "verbosityy",
        "compact",
        "verbosity",
    ),
    "segment": (
        {"segment_id": "segment.jaffle.high_value_customers", "action": "validate"},
        "segment_idd",
        "segment.jaffle.x",
        "segment_id",
    ),
}


def test_all_tools_classified_strict_or_warn() -> None:
    declared = {definition.name for definition in TOOL_DEFINITIONS}
    assert declared == _STRICT_REJECT_TOOLS | _WARN_AND_IGNORE_TOOLS == set(_CASES)
    assert not (_STRICT_REJECT_TOOLS & _WARN_AND_IGNORE_TOOLS)
    assert set(_UNKNOWN_ARG_WARNING_CODE) >= _WARN_AND_IGNORE_TOOLS


@pytest.mark.parametrize("tool_name", sorted(_CASES))
def test_unknown_arguments_warn_or_reject_with_the_closest_match(runtime_factory, tool_name):
    minimum, typo, value, expected = _CASES[tool_name]
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        out = adapter.call_tool(tool_name, {**minimum, "garbage_key_xyz": "x", typo: value})
    finally:
        adapter.close()
    if tool_name in _STRICT_REJECT_TOOLS:
        assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
        details = out["errors"][0]["details"]
        assert {"garbage_key_xyz", typo} <= set(details["unknown_keys"])
        assert expected in details["closest_matches"]
        return
    details = [w.get("details") or {} for w in out.get("warnings") or [] if isinstance(w, dict)]
    codes = {d.get("received"): w["code"] for d, w in zip(details, out["warnings"], strict=True)}
    assert codes["garbage_key_xyz"] == _UNKNOWN_ARG_WARNING_CODE[tool_name]
    # The typo's suggestion is on the boundary warning, or as ``expected`` on a
    # handler's own typo warning.
    suggested = [
        name
        for d in details
        if d.get("received") == typo
        for name in [*(d.get("closest_matches") or []), d.get("expected")]
    ]
    assert expected in suggested, out["warnings"]
