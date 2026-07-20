"""MCP unknown-argument uniformity tests.

After blind-agent probing, the MCP tools no longer
silently accept unknown arguments. Each tool either:

* **strict-reject** — raises ``INVALID_MCP_ARGUMENTS`` with
  ``closest_matches`` (capabilities, catalog, segment-*); or
* **warn-and-ignore** — emits a ``<TOOL>_UNKNOWN_ARG`` warning with
  ``closest_matches`` and continues (discover, build-options,
  inspect, valid-values, plan, validate, compile, execute).

These tests pin that contract per tool and verify that ``policy_context``
and top-level Query-IR passthrough still work on the warn-tools.
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

# ----------------------------------------------------------------------
# Minimum-required-args per tool. Each entry is the smallest payload
# that satisfies the tool's required fields — used as a base for the
# unknown-arg test cases below.
# ----------------------------------------------------------------------

_MINIMUM_ARGS: dict[str, dict[str, Any]] = {
    "capabilities": {},
    "catalog": {},
    "discover": {"terms": "revenue"},
    "inspect": {"object_id": "measure.jaffle.revenue_usd"},
    "build-options": {},
    "valid-values": {"dimension_id": "dimension.jaffle.store_name"},
    "plan": {"intent": "revenue"},
    "validate": {
        "query": {"select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "r"}]}
    },
    "compile": {
        "query": {"select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "r"}]}
    },
    "execute": {
        "query": {"select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "r"}]}
    },
    "segment-validate": {"segment_id": "segment.jaffle.high_value_customers"},
    "segment-explain": {"segment_id": "segment.jaffle.high_value_customers"},
    "segment-preview": {"segment_id": "segment.jaffle.high_value_customers"},
}

# Per-tool known-typo case: (typo_key, typo_value, expected_closest_match).
# The expected match is an existing legitimate argument the typo should
# resolve to via ``difflib.get_close_matches``.
_KNOWN_TYPOS: dict[str, tuple[str, Any, str]] = {
    "capabilities": ("request_idd", "x", "request_id"),
    "catalog": ("verbosityy", "compact", "verbosity"),
    "discover": ("limitt", 5, "limit"),
    "inspect": ("object_ids", "measure.jaffle.revenue_usd", "object_id"),
    "build-options": ("focus_object", "x", "focus_object_id"),
    "valid-values": ("dimension", "x", "dimension_id"),
    "plan": ("intentt", "revenue", "intent"),
    "validate": ("verbosityy", "compact", "verbosity"),
    "compile": ("verbosityy", "compact", "verbosity"),
    "execute": ("verbosityy", "compact", "verbosity"),
    "segment-validate": ("segment_idd", "segment.jaffle.x", "segment_id"),
    "segment-explain": ("segment_idd", "segment.jaffle.x", "segment_id"),
    "segment-preview": ("limitt", 5, "limit"),
}


# Sanity guard — if a new tool is added to TOOL_DEFINITIONS without
# wiring it into the strictness tables, this assertion catches it.
def test_all_tools_classified_strict_or_warn() -> None:
    declared = {definition.name for definition in TOOL_DEFINITIONS}
    classified = _STRICT_REJECT_TOOLS | _WARN_AND_IGNORE_TOOLS
    assert declared == classified, (
        f"Every MCP tool must be classified strict-reject or warn-ignore. "
        f"Missing: {declared - classified}; extra: {classified - declared}."
    )
    # Warn-tools must have a registered warning code.
    for name in _WARN_AND_IGNORE_TOOLS:
        assert name in _UNKNOWN_ARG_WARNING_CODE
    # Strict-reject and warn-and-ignore are disjoint.
    assert not (_STRICT_REJECT_TOOLS & _WARN_AND_IGNORE_TOOLS)


def _expect_warn_or_reject_for_bogus_key(
    out: dict[str, Any], tool_name: str, bogus_key: str
) -> None:
    if tool_name in _STRICT_REJECT_TOOLS:
        assert out["ok"] is False
        assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
        details = out["error"].get("details", {}) or {}
        unknown = details.get("unknown_keys") or []
        assert bogus_key in unknown, (
            f"{tool_name}: expected {bogus_key!r} in unknown_keys, got {unknown}"
        )
    else:
        warnings = out.get("warnings") or []
        codes = [w.get("code") for w in warnings if isinstance(w, dict)]
        expected_code = _UNKNOWN_ARG_WARNING_CODE[tool_name]
        assert expected_code in codes, (
            f"{tool_name}: expected {expected_code} in warnings, got {codes}"
        )
        received = [
            (w.get("details") or {}).get("received") for w in warnings if isinstance(w, dict)
        ]
        assert bogus_key in received, (
            f"{tool_name}: expected {bogus_key!r} in warning details.received, got {received}"
        )


@pytest.mark.parametrize("tool_name", sorted({definition.name for definition in TOOL_DEFINITIONS}))
def test_bogus_key_warns_or_rejects(runtime_factory, tool_name: str) -> None:
    """Every tool either warns or rejects a clearly-bogus key — no tool
    silently accepts unknown args.
    """
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        args = dict(_MINIMUM_ARGS[tool_name])
        args["garbage_key_xyz"] = "x"
        out = adapter.call_tool(tool_name, args)
    finally:
        adapter.close()
    _expect_warn_or_reject_for_bogus_key(out, tool_name, "garbage_key_xyz")


@pytest.mark.parametrize("tool_name", sorted({definition.name for definition in TOOL_DEFINITIONS}))
def test_known_typo_returns_closest_match(runtime_factory, tool_name: str) -> None:
    """A typo of a real arg surfaces the canonical name in
    ``closest_matches`` — either inside the error envelope (strict) or
    the warning details (warn-and-ignore).
    """
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        args = dict(_MINIMUM_ARGS[tool_name])
        typo_key, typo_value, expected_match = _KNOWN_TYPOS[tool_name]
        args[typo_key] = typo_value
        out = adapter.call_tool(tool_name, args)
    finally:
        adapter.close()
    if tool_name in _STRICT_REJECT_TOOLS:
        assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
        closest = (out["error"].get("details") or {}).get("closest_matches") or []
        assert expected_match in closest, (
            f"{tool_name}: expected {expected_match!r} in closest_matches, got {closest}"
        )
    else:
        warnings = out.get("warnings") or []
        # Find the warning that flagged this typo.
        matching = [
            w
            for w in warnings
            if isinstance(w, dict) and (w.get("details") or {}).get("received") == typo_key
        ]
        assert matching, (
            f"{tool_name}: no warning surfaced for typo {typo_key!r}; got warnings={warnings}"
        )
        # closest_matches may live on the generic boundary warning
        # (via _unknown_argument_warnings) OR on a handler-emitted
        # typo warning that uses ``expected`` plus closest_matches. The
        # first matching warning that mentions ``expected_match`` wins.
        joined = []
        for w in matching:
            details = w.get("details") or {}
            joined.extend(details.get("closest_matches") or [])
            expected_field = details.get("expected")
            if expected_field:
                joined.append(expected_field)
        assert expected_match in joined, (
            f"{tool_name}: expected {expected_match!r} in any of "
            f"closest_matches / expected; got {joined} from {matching}"
        )


@pytest.mark.parametrize("tool_name", sorted(_WARN_AND_IGNORE_TOOLS))
def test_policy_context_passthrough_no_warning(runtime_factory, tool_name: str) -> None:
    """``policy_context`` is legitimate on every warn-tool — passing
    it must not trigger an unknown-arg warning.
    """
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        args = dict(_MINIMUM_ARGS[tool_name])
        args["policy_context"] = {"environment": "staging", "audience": "internal"}
        args["request_id"] = "rq-policy-ctx"
        out = adapter.call_tool(tool_name, args)
    finally:
        adapter.close()
    warnings = out.get("warnings") or []
    unknown_arg_codes = [
        w.get("code")
        for w in warnings
        if isinstance(w, dict) and str(w.get("code", "")).endswith("_UNKNOWN_ARG")
    ]
    assert not unknown_arg_codes, (
        f"{tool_name}: policy_context passthrough must not trigger "
        f"unknown-arg warnings; got {unknown_arg_codes}"
    )
    # Direct adapter calls are the trusted stdio/in-process boundary. Remote
    # transports replace these values, but local callers retain the existing
    # ability to provide policy context explicitly.
    assert out["request_context"]["environment"] == "staging"
    assert out["request_context"]["audience"] == "internal"


@pytest.mark.parametrize("tool_name", ["validate", "compile", "execute"])
def test_top_level_ir_passthrough_no_warning(runtime_factory, tool_name: str) -> None:
    """Callers who skip the ``query`` wrapper and pass canonical IR
    keys at top-level (``select``, ``time``, ``version``) must not
    trigger unknown-arg warnings — that shortcut is part of the
    documented contract.
    """
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        args: dict[str, Any] = {
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.revenue_usd"},
                    "as": "revenue",
                }
            ],
            "version": 1,
            "request_id": "rq-top-level-ir",
        }
        out = adapter.call_tool(tool_name, args)
    finally:
        adapter.close()
    warnings = out.get("warnings") or []
    unknown_arg_codes = [
        w.get("code")
        for w in warnings
        if isinstance(w, dict) and str(w.get("code", "")).endswith("_UNKNOWN_ARG")
    ]
    assert not unknown_arg_codes, (
        f"{tool_name}: top-level IR passthrough must not trigger "
        f"unknown-arg warnings; got {unknown_arg_codes}"
    )
