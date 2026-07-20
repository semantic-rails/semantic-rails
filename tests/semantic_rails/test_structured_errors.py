"""Bare ``KeyError`` and other unstructured exceptions used to leak
through the JSON-RPC layer as ``MCP error -32603: 'field'`` with no
``code``, no ``recovery_hints``, and no ``closest_matches``. This file
locks in the structured-error envelope for the failure modes the
reviewer reported, plus the per-expression-position unknown-key check.
"""

from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import expression_field, parse_semantic_expression
from semantic_rails.mcp import SemanticLayerMCPAdapter

# ---------------------------------------------------------------------
# Direct unit checks — exercise the helpers without the full runtime.
# ---------------------------------------------------------------------


def test_expression_field_raises_invalid_expression_when_dict_lacks_key():
    with pytest.raises(SemanticLayerError) as exc:
        expression_field({"direction": "DESC"}, "field", expression_position="order_by")
    assert exc.value.code == "INVALID_EXPRESSION"
    details = exc.value.details
    assert details["expression_position"] == "order_by"
    assert details["missing_key"] == "field"
    assert "direction" in details["received_keys"]


def test_expression_field_raises_when_value_is_not_a_dict():
    with pytest.raises(SemanticLayerError) as exc:
        expression_field("not_a_dict", "field", expression_position="order_by")
    assert exc.value.code == "INVALID_EXPRESSION"
    assert exc.value.details["received_type"] == "str"


def test_order_by_without_field_returns_structured_envelope_via_normalize_query():
    """The reviewer's failing case: ``order_by`` with a select-shape
    expression instead of ``{field, direction}``. Previously this raised
    a bare ``KeyError('field')``; now it must surface a structured
    ``INVALID_EXPRESSION`` envelope with usable recovery hints.
    """
    payload = {
        "select": [{"expression": {"measure": "measure.x"}, "as": "x"}],
        "order_by": [{"expression": {"measure": "measure.x"}, "direction": "DESC"}],
    }
    with pytest.raises(SemanticLayerError) as exc:
        normalize_query(payload)
    assert exc.value.code == "INVALID_EXPRESSION"
    assert exc.value.details["expression_position"] == "order_by"
    assert exc.value.details["missing_key"] == "field"
    # And the recovery_hints path must offer the right shape.
    hints = recovery_hints_for_error(exc.value.code, exc.value.details)
    assert hints, "recovery_hints must be non-empty for INVALID_EXPRESSION"
    kinds = {h.get("kind") for h in hints}
    assert "fix_order_by_shape" in kinds
    hint = next(h for h in hints if h.get("kind") == "fix_order_by_shape")
    assert hint["shape"]["field"], "hint must include the expected order_by shape"


def test_unknown_top_level_expression_key_returns_invalid_expression_key():
    """``parse_semantic_expression`` rejects unknown top-level keys
    inside any expression position with ``INVALID_EXPRESSION_KEY`` plus
    a ``closest_matches`` block.
    """
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {"measure": "measure.x", "meaure": "typo_for_measure"}, context="query"
        )
    assert exc.value.code == "INVALID_EXPRESSION_KEY"
    details = exc.value.details
    assert details["expression_kind"] == "measure"
    assert "meaure" in details["unknown_keys"]
    # closest_matches must point at the intended key.
    assert "measure" in details["closest_matches"]
    # And the recovery_hints path must propagate this.
    hints = recovery_hints_for_error(exc.value.code, exc.value.details)
    assert hints
    hint = hints[0]
    assert hint["kind"] == "remove_or_rename_expression_key"
    assert "measure" in hint["closest_matches"]


def test_unknown_key_on_kinded_expression_is_rejected():
    """Same protection applies when ``kind`` is explicit — a bogus key
    on a ``kind=metric`` expression must surface ``INVALID_EXPRESSION_KEY``.
    """
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(
            {"kind": "metric", "metric": "metric.x", "filter": {"unsupported": True}},
            context="query",
        )
    assert exc.value.code == "INVALID_EXPRESSION_KEY"
    assert "filter" in exc.value.details["unknown_keys"]


def test_kinded_expression_with_only_valid_keys_passes_unknown_key_check():
    """Negative control: a well-formed ``kind=metric`` expression must
    not trip the unknown-key check.
    """
    parsed = parse_semantic_expression({"kind": "metric", "metric": "metric.x"}, context="query")
    # No exception — just confirm we got back a parsed node.
    assert getattr(parsed, "metric_recipe", "") == "metric.x"


# ---------------------------------------------------------------------
# End-to-end via the MCP adapter — confirms the structured envelope
# survives the JSON-RPC boundary (this is the surface the reviewer hit).
# ---------------------------------------------------------------------


def _first_error(out: dict) -> dict:
    """``validate`` returns ``ok=False`` with the structured issue in
    ``errors[0]``; ``compile`` raises (caught by the adapter and packed
    into ``error`` at the top level). Return whichever shape is present.
    """
    if isinstance(out.get("error"), dict):
        return out["error"]
    errors = list(out.get("errors", []) or [])
    return errors[0] if errors else {}


def test_mcp_validate_with_bad_order_by_returns_structured_envelope(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool(
            "validate",
            {
                "query": {
                    "version": 1,
                    "select": [
                        {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "rev"}
                    ],
                    "order_by": [
                        {
                            "expression": {"measure": "measure.jaffle.revenue_usd"},
                            "direction": "DESC",
                        }
                    ],
                }
            },
        )
    finally:
        adapter.close()
    assert out["ok"] is False
    issue = _first_error(out)
    assert issue.get("code") == "INVALID_EXPRESSION", out
    assert out["recovery_hints"], "MCP envelope must carry recovery_hints"
    kinds = {h.get("kind") for h in out["recovery_hints"]}
    assert "fix_order_by_shape" in kinds


def test_mcp_compile_with_bad_order_by_returns_structured_envelope(runtime_factory):
    """``compile`` raises rather than soft-failing — the adapter must
    still pack the structured envelope (this is the surface the reviewer
    reported as 'MCP error -32603: field').
    """
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool(
            "compile",
            {
                "query": {
                    "version": 1,
                    "select": [
                        {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "rev"}
                    ],
                    "order_by": [
                        {
                            "expression": {"measure": "measure.jaffle.revenue_usd"},
                            "direction": "DESC",
                        }
                    ],
                }
            },
        )
    finally:
        adapter.close()
    assert out["ok"] is False
    issue = _first_error(out)
    assert issue.get("code") == "INVALID_EXPRESSION", out
    assert out["recovery_hints"]
    # The whole point of the structured envelope — no bare KeyError.
    assert "field" in (issue.get("details", {}).get("missing_key", "") or "")


def test_mcp_validate_with_unknown_expression_key_returns_closest_matches(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool(
            "validate",
            {
                "query": {
                    "version": 1,
                    "select": [
                        {
                            "expression": {
                                "measure": "measure.jaffle.revenue_usd",
                                "meaure": "typo",  # typo intended
                            },
                            "as": "rev",
                        }
                    ],
                }
            },
        )
    finally:
        adapter.close()
    assert out["ok"] is False
    issue = _first_error(out)
    assert issue.get("code") == "INVALID_EXPRESSION_KEY", out
    hints = out["recovery_hints"]
    assert hints
    closest = hints[0].get("closest_matches", [])
    assert "measure" in closest
