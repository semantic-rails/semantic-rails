"""MCP adapter token-budget regressions: minimal-by-default envelopes.

A blind-agent evaluation (2026-06) measured the validate/compile/execute
MCP tools returning 91KB/99KB/103KB envelopes at their old default
verbosity ('compact', inherited from the HTTP v1 surface), and a single
failed call burning 62KB of a context-constrained agent's window. The
MCP adapter now defaults those three tools to verbosity='minimal':

  * validate -> {ok, errors, warnings}
  * compile  -> {ok, errors, warnings, rendered_sql}
  * execute  -> {ok, errors, warnings, rows, row_count}

(plus the standard MCP envelope keys: status, request_id, recovery_hints,
...). Explicit `verbosity` arguments still win, and the HTTP v1 surface
is untouched — these tests pin the MCP boundary only.

Also pinned here: a 24KB tools/list budget (post IR cheat-sheet +
QUERY_SCHEMA dedupe the measured size is ~16KB) and the <6KB wire size
of a default-args execute on a 2-row query.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from semantic_rails.mcp import (
    MCP_DEFAULT_QUERY_VERBOSITY,
    MCP_TOOL_DEFINITIONS,
    SemanticLayerMCPAdapter,
)

# Two-row query: jaffle_shop has more than two stores; limit pins rows=2.
_TWO_ROW_QUERY: dict[str, Any] = {
    "version": 1,
    "select": [
        {
            "expression": {
                "measure": "measure.jaffle.order_count",
                "aggregation": "count_distinct",
            },
            "as": "orders",
        }
    ],
    "group_by": ["dimension.jaffle_store_name"],
    "limit": 2,
}

# Heavy keys that must never appear on a default (minimal) envelope.
_HEAVY_KEYS = (
    "logical_plan",
    "sql_plan",
    "physical_plan",
    "semantic_summary",
    "performance_plan",
    "compile_stats",
    "explain",
    "normalized_query",
    "output_columns",
    "freshness",
)


def _wire_bytes(envelope: dict[str, Any]) -> int:
    return len(json.dumps(envelope, default=str))


def test_mcp_default_verbosity_constant_is_minimal() -> None:
    assert MCP_DEFAULT_QUERY_VERBOSITY == "minimal"


def test_tool_schemas_advertise_minimal_default() -> None:
    """The inputSchema default must match the runtime behavior so agents
    reading tools/list aren't lied to."""
    for tool in MCP_TOOL_DEFINITIONS:
        if tool["name"] not in {"validate", "compile", "execute"}:
            continue
        verbosity = tool["inputSchema"]["properties"]["verbosity"]
        assert verbosity["default"] == "minimal", (
            f"{tool['name']} inputSchema verbosity default must be 'minimal'; "
            f"got {verbosity['default']!r}"
        )


def test_validate_defaults_to_minimal_envelope(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        default = adapter.call_tool("validate", {"query": dict(_TWO_ROW_QUERY)})
        explicit = adapter.call_tool(
            "validate", {"query": dict(_TWO_ROW_QUERY), "verbosity": "minimal"}
        )
        assert default["ok"] is True
        for key in (*_HEAVY_KEYS, "rendered_sql"):
            assert key not in default, f"default validate envelope leaked {key}"
        # Default and explicit-minimal must expose the same payload keys.
        assert set(default) - {"request_id", "timing_ms"} == set(explicit) - {
            "request_id",
            "timing_ms",
        }
        # Explicit verbosity still wins over the minimal default.
        compact = adapter.call_tool(
            "validate", {"query": dict(_TWO_ROW_QUERY), "verbosity": "compact"}
        )
        assert "normalized_query" in compact, "explicit verbosity=compact must win"
    finally:
        adapter.close()


def test_compile_defaults_to_minimal_but_keeps_rendered_sql(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        default = adapter.call_tool("compile", {"query": dict(_TWO_ROW_QUERY)})
        assert default["ok"] is True
        assert default.get("rendered_sql"), (
            "minimal compile must keep rendered_sql — it is the tool's product"
        )
        for key in _HEAVY_KEYS:
            assert key not in default, f"default compile envelope leaked {key}"
    finally:
        adapter.close()


def test_execute_default_args_returns_rows_under_six_kb(runtime_factory) -> None:
    """The headline regression: an execute with DEFAULT args must return
    rows + row_count and stay under ~6KB wire size for a 2-row query
    (it measured 103KB before the minimal default)."""
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        envelope = adapter.call_tool("execute", {"query": dict(_TWO_ROW_QUERY)})
        assert envelope["ok"] is True
        assert isinstance(envelope.get("rows"), list)
        assert len(envelope["rows"]) == 2
        assert envelope.get("row_count") == 2
        for key in (*_HEAVY_KEYS, "rendered_sql"):
            assert key not in envelope, f"default execute envelope leaked {key}"
        size = _wire_bytes(envelope)
        assert size < 6_000, (
            f"default-args execute envelope is {size:,}B for a 2-row query; "
            "the MCP minimal default must keep it under 6KB."
        )
    finally:
        adapter.close()


@pytest.mark.parametrize("tool_name", ["validate", "compile", "execute"])
def test_error_envelopes_are_minimal_by_default(runtime_factory, tool_name: str) -> None:
    """Failed calls must not cost more than successful ones — the blind
    agent burned 62KB on a single failed default-verbosity call. A bad
    grain through any of the three tools must come back small, while
    still carrying the structured errors + recovery_hints contract."""
    bad_query = {
        "version": 1,
        "select": [
            {
                "expression": {"measure": "measure.jaffle.order_count"},
                "as": "orders",
            }
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "fortnight",
        },
    }
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        envelope = adapter.call_tool(tool_name, {"query": bad_query})
        assert envelope["ok"] is False
        assert envelope.get("errors"), "structured errors must survive minimal gating"
        assert envelope.get("recovery_hints"), (
            "recovery_hints must survive minimal gating — that is the loop-repair signal"
        )
        for key in _HEAVY_KEYS:
            assert key not in envelope, f"default {tool_name} error envelope leaked {key}"
        size = _wire_bytes(envelope)
        assert size < 8_000, (
            f"default-verbosity {tool_name} ERROR envelope is {size:,}B; "
            "failed calls must stay cheap for context-constrained agents."
        )
    finally:
        adapter.close()


def test_plan_next_block_inherits_minimal_default(runtime_factory) -> None:
    """plan's pre-baked `next.validate` args must not pin an explicit
    verbosity — forwarding them verbatim should inherit the MCP
    adapter's minimal default, keeping the planned loop cheap."""
    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        plan = adapter.call_tool("plan", {"intent": "orders by store"})
        next_block = plan.get("next") or {}
        validate_args = next_block.get("validate") or {}
        assert "query" in validate_args, "plan.next.validate must pre-bake the query"
        assert "verbosity" not in validate_args
        assert "verbosity" not in (validate_args.get("query") or {})
        forwarded = adapter.call_tool("validate", validate_args)
        for key in (*_HEAVY_KEYS, "rendered_sql"):
            assert key not in forwarded, (
                f"forwarding plan.next.validate leaked {key} — the pre-baked "
                "args must inherit the minimal MCP default"
            )
    finally:
        adapter.close()


def test_tools_list_under_24kb_budget() -> None:
    """tools/list measured 28.1KB before the IR cheat-sheet and full
    QUERY_SCHEMA were deduped onto 'validate' (~16KB after). Pin a 24KB
    ceiling so prose accretion can't silently re-bloat the first thing
    every fresh agent reads."""
    total = sum(len(json.dumps(t, separators=(",", ":"))) for t in MCP_TOOL_DEFINITIONS)
    assert total < 24_000, (
        f"tools/list JSON is {total:,}B — over the 24,000B budget. "
        "Dedupe schemas/descriptions (the IR cheat-sheet and full "
        "QUERY_SCHEMA live on 'validate' only) or raise the cap "
        "explicitly with a comment."
    )
