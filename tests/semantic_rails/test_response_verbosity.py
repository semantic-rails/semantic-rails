"""Phase 1 — verbosity + sql_profile gating on validate/compile/explain/execute.

The pre-Phase-1 envelopes shipped every heavy field unconditionally —
``physical_plan``, ``performance_plan``, ``semantic_summary``,
``compile_stats``, and ``output_column.lineage`` — so a typical
response was 50–120 KB even when the answer was a handful of rows.
After Phase 1:

- Default verbosity is ``compact`` (was effectively ``full``) and
  drops those heavy keys while keeping ``rendered_sql`` and the
  ``sql_plan`` outline.
- ``verbosity="minimal"`` strips to ``{ok, errors, warnings}`` (plus
  ``rows`` / ``row_count`` for execute) so an agent loop with a 25K
  token tool-output cap can iterate without blowing the budget.
- ``verbosity="full"`` returns the maximal envelope (every heavy field).
- ``sql_profile="off"`` drops ``rendered_sql`` and ``sql_plan`` at any
  verbosity for callers that want only the semantic envelope.

Mirrors the pattern from ``test_plan_verbosity.py`` but for the
four heavyweight endpoints.
"""

from __future__ import annotations

import json

import pytest

_BASE_QUERY = {
    "version": 1,
    "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
    "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    "limit": 3,
}


_COMPACT_DROPPED_KEYS = (
    "physical_plan",
    "performance_plan",
    "semantic_summary",
    "compile_stats",
)


_MINIMAL_NONEXECUTE_KEYS = frozenset({"ok", "errors", "warnings"})
_MINIMAL_COMPILE_KEYS = frozenset({"ok", "errors", "warnings", "rendered_sql"})
_MINIMAL_EXECUTE_KEYS = frozenset({"ok", "rows", "row_count", "truncated", "errors", "warnings"})


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_minimal_keeps_only_ok_errors_warnings(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate({**_BASE_QUERY, "verbosity": "minimal"})

        # Only the minimal keys remain.
        assert set(result.keys()) <= _MINIMAL_NONEXECUTE_KEYS, (
            f"unexpected keys in minimal validate response: {sorted(set(result) - _MINIMAL_NONEXECUTE_KEYS)}"
        )
        assert result["ok"] is True
        assert result["errors"] == []
        assert isinstance(result["warnings"], list)

        # Minimal responses are tiny.
        assert len(json.dumps(result, default=str)) < 2_000
    finally:
        runtime.close()


def test_validate_compact_drops_heavy_keys(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # Compact is the new default — no explicit verbosity needed.
        compact = runtime.validate(dict(_BASE_QUERY))

        # Heavy keys gone.
        for key in _COMPACT_DROPPED_KEYS:
            assert key not in compact, f"compact validate must drop {key}"

        # output_columns present but lineage stripped.
        assert "output_columns" in compact
        for column in compact["output_columns"]:
            assert "lineage" not in column, "compact must strip output_column.lineage"

        # validate excludes rendered_sql/sql_plan — the
        # `explain.rendered_sql` field carries the SQL instead. Compact
        # must preserve that shape.
        assert compact.get("sql_profile") == "audit"
        assert "rendered_sql" not in compact
        assert isinstance(compact.get("explain", {}).get("rendered_sql"), str)

        # Status / ok envelope intact.
        assert compact["ok"] is True
        assert compact["status"] == "ok"
    finally:
        runtime.close()


def test_validate_full_preserves_legacy_envelope(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate({**_BASE_QUERY, "verbosity": "full"})

        # Every heavy key must be back.
        for key in ("compile_stats", "performance_plan", "semantic_summary"):
            assert key in result, f"full validate must keep {key}"

        # output_column.lineage must be present in full mode.
        assert result["output_columns"]
        assert "lineage" in result["output_columns"][0]
    finally:
        runtime.close()


def test_validate_sql_profile_off_strips_rendered_sql(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # sql_profile=off works at any verbosity. Test at compact (default).
        result = runtime.validate({**_BASE_QUERY, "sql_profile": "off"})

        assert result.get("sql_profile") == "off"
        assert "rendered_sql" not in result
        # validate doesn't expose sql_plan but the off contract should
        # hold even if it did.
        assert "sql_plan" not in result
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------


def test_compile_minimal_keeps_sql_plus_envelope(runtime_factory):
    """Compile's minimal payload keeps ``rendered_sql`` — that's the
    whole product of compile. Earlier minimal output stripped the SQL
    too, leaving callers with a useless envelope (caught by the blind-
    agent benchmark, where "minimal compile" had nothing but ok/errors).
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.compile({**_BASE_QUERY, "verbosity": "minimal"})

        assert set(result.keys()) <= _MINIMAL_COMPILE_KEYS
        assert result["ok"] is True
        assert "rendered_sql" in result
        assert isinstance(result["rendered_sql"], str) and result["rendered_sql"]
        # Heavy plan introspection is still dropped at minimal verbosity.
        assert "sql_plan" not in result
        assert "logical_plan" not in result
        # Compile minimal stays small — well under the 25K-token tool-output budget.
        assert len(json.dumps(result, default=str)) < 6_000
    finally:
        runtime.close()


def test_compile_compact_drops_heavy_keys(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.compile(dict(_BASE_QUERY))

        for key in _COMPACT_DROPPED_KEYS:
            assert key not in result, f"compact compile must drop {key}"

        # sql_plan is the minimal SQL overview — still present in compact.
        assert "sql_plan" in result
        assert isinstance(result.get("rendered_sql"), str) and result["rendered_sql"]
        assert "trace" in result
        assert result["trace"]["selected"]["subjects"]
        assert "physical_plan" not in result["trace"]
        # output_columns present but lineage stripped.
        assert "output_columns" in result
        for column in result["output_columns"]:
            assert "lineage" not in column
    finally:
        runtime.close()


def test_compile_full_preserves_legacy_envelope(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.compile({**_BASE_QUERY, "verbosity": "full"})

        for key in ("compile_stats", "performance_plan", "semantic_summary", "physical_plan"):
            assert key in result, f"full compile must keep {key}"

        assert result["output_columns"]
        assert "lineage" in result["output_columns"][0]
    finally:
        runtime.close()


def test_compile_sql_profile_off_strips_sql_and_plan(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.compile({**_BASE_QUERY, "sql_profile": "off"})

        assert result.get("sql_profile") == "off"
        assert "rendered_sql" not in result
        assert "sql_plan" not in result
        # The semantic envelope (status, query, normalized_query, etc.)
        # must still be present so callers retain methodology context.
        assert result["status"] == "ok"
        assert "normalized_query" in result
    finally:
        runtime.close()


def test_compile_sql_profile_off_at_full_verbosity_still_strips(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # Verbosity full keeps the heavy plans, but sql_profile=off must
        # still suppress rendered_sql + sql_plan.
        result = runtime.compile({**_BASE_QUERY, "verbosity": "full", "sql_profile": "off"})

        assert "rendered_sql" not in result
        assert "sql_plan" not in result
        # Heavy keys still present at full.
        assert "physical_plan" in result
        assert "performance_plan" in result
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# execute (query)
# ---------------------------------------------------------------------------


def test_execute_minimal_keeps_rows_and_envelope(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query({**_BASE_QUERY, "verbosity": "minimal"})

        # rows / row_count must survive even in minimal mode.
        assert set(result.keys()) <= _MINIMAL_EXECUTE_KEYS
        assert isinstance(result["rows"], list)
        assert result["row_count"] == len(result["rows"])
        assert result["ok"] is True
        assert "rendered_sql" not in result
        assert "physical_plan" not in result
    finally:
        runtime.close()


def test_execute_compact_drops_heavy_keys_but_keeps_rows_and_sql(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query(dict(_BASE_QUERY))

        for key in _COMPACT_DROPPED_KEYS:
            assert key not in result, f"compact execute must drop {key}"

        assert isinstance(result["rows"], list)
        assert result["row_count"] == len(result["rows"])
        assert isinstance(result.get("rendered_sql"), str)
        # sql_plan minimal overview still present
        assert "sql_plan" in result
        assert "trace" in result
        assert result["trace"]["selected"]["subjects"]
        assert "physical_plan" not in result["trace"]
        # output_columns present but lineage stripped
        assert "output_columns" in result
        for column in result["output_columns"]:
            assert "lineage" not in column
    finally:
        runtime.close()


def test_execute_full_preserves_legacy_envelope(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query({**_BASE_QUERY, "verbosity": "full"})

        for key in ("compile_stats", "performance_plan", "semantic_summary", "physical_plan"):
            assert key in result, f"full execute must keep {key}"
        assert result["output_columns"]
        assert "lineage" in result["output_columns"][0]
    finally:
        runtime.close()


def test_execute_sql_profile_off_strips_rendered_sql_and_plan(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query({**_BASE_QUERY, "sql_profile": "off"})

        assert result.get("sql_profile") == "off"
        assert "rendered_sql" not in result
        assert "sql_plan" not in result
        # Rows + envelope must remain.
        assert isinstance(result["rows"], list)
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# MCP + HTTP plumbing
# ---------------------------------------------------------------------------


def test_mcp_validate_hoists_verbosity_from_outer_arguments(runtime_factory):
    """The MCP adapter accepts verbosity at the outer argument layer, not
    just inside ``query``. The Phase 1 plan calls out matching the
    catalog plumbing pattern.
    """
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        envelope = adapter.call_tool(
            "validate",
            {"query": dict(_BASE_QUERY), "verbosity": "minimal"},
        )
        # The MCP envelope adds status/ok/api_version/etc., but the
        # validate-shaped payload it wraps must reflect minimal gating.
        assert envelope["ok"] is True
        for key in _COMPACT_DROPPED_KEYS:
            assert key not in envelope, f"minimal MCP envelope leaked {key}"
        assert "rendered_sql" not in envelope
        assert "logical_plan" not in envelope
    finally:
        adapter.close()


def test_mcp_execute_hoists_sql_profile_off(runtime_factory):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        # Pin compact explicitly: the MCP adapter defaults to
        # verbosity='minimal', which would strip the sql_profile echo
        # this test asserts on.
        envelope = adapter.call_tool(
            "execute",
            {"query": dict(_BASE_QUERY), "sql_profile": "off", "verbosity": "compact"},
        )
        assert envelope["ok"] is True
        assert envelope.get("sql_profile") == "off"
        assert "rendered_sql" not in envelope
        assert "sql_plan" not in envelope
        assert isinstance(envelope.get("rows"), list)
    finally:
        adapter.close()


def test_mcp_execute_defaults_to_record_rows(runtime_factory):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        envelope = adapter.call_tool("execute", {"query": dict(_BASE_QUERY)})
        assert envelope["ok"] is True
        assert "row_format" not in envelope
        assert "columns" not in envelope
        assert isinstance(envelope["rows"], list)
        assert envelope["rows"]
        assert isinstance(envelope["rows"][0], dict)
        assert envelope["row_count"] == len(envelope["rows"])
    finally:
        adapter.close()


def test_mcp_execute_columns_row_format_preserves_values_and_counts(runtime_factory):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    query = {**_BASE_QUERY, "order_by": [{"field": "time", "direction": "ASC"}]}
    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        records = adapter.call_tool("execute", {"query": dict(query)})
        columns = adapter.call_tool(
            "execute",
            {"query": dict(query), "row_format": "columns"},
        )
    finally:
        adapter.close()

    assert records["ok"] is True
    assert columns["ok"] is True
    assert columns["row_format"] == "columns"
    assert columns["row_count"] == records["row_count"]
    assert columns["warnings"] == records["warnings"]
    assert columns["errors"] == records["errors"]
    assert columns["columns"] == list(records["rows"][0].keys())
    assert columns["rows"] == [
        [record.get(column) for column in columns["columns"]] for record in records["rows"]
    ]


def test_mcp_execute_rejects_invalid_row_format(runtime_factory):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        envelope = adapter.call_tool(
            "execute",
            {"query": dict(_BASE_QUERY), "row_format": "tuples"},
        )
    finally:
        adapter.close()

    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    assert envelope["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
    assert envelope["error"]["details"]["field"] == "row_format"


def test_mcp_execute_top_level_ir_row_format_does_not_leak_to_runtime(runtime_factory):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        envelope = adapter.call_tool("execute", {**_BASE_QUERY, "row_format": "columns"})
    finally:
        adapter.close()

    assert envelope["ok"] is True
    assert envelope["row_format"] == "columns"
    assert isinstance(envelope["rows"], list)
    assert envelope["rows"]
    assert isinstance(envelope["rows"][0], list)
    if "query" in envelope:
        assert "row_format" not in envelope["query"]
    assert not any(error.get("code") == "INVALID_QUERY" for error in envelope.get("errors", []))


def test_http_query_payload_hoists_verbosity_and_sql_profile():
    """The HTTP shim must mirror the MCP hoisting so /validate, /compile,
    /explain, /query all honour the outer-envelope keys.
    """
    from semantic_rails.http_core import query_payload

    body = {
        "query": {"version": 1, "select": []},
        "verbosity": "minimal",
        "sql_profile": "off",
    }
    out = query_payload(body)
    assert out["verbosity"] == "minimal"
    assert out["sql_profile"] == "off"


def test_http_query_payload_in_query_values_win_over_outer():
    """A caller can override verbosity inside the Query IR itself; the
    outer envelope is only a default for callers that don't.
    """
    from semantic_rails.http_core import query_payload

    body = {
        "query": {"version": 1, "select": [], "verbosity": "full"},
        "verbosity": "minimal",
    }
    out = query_payload(body)
    assert out["verbosity"] == "full"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["validate", "compile", "execute"])
def test_mcp_tool_schema_advertises_verbosity_and_sql_profile(tool_name: str):
    from semantic_rails.mcp import MCP_TOOL_DEFINITIONS

    matches = [row for row in MCP_TOOL_DEFINITIONS if row["name"] == tool_name]
    assert matches, f"tool {tool_name} not registered"
    schema = matches[0]["inputSchema"]
    properties = schema.get("properties", {})

    verbosity = properties.get("verbosity") or {}
    assert verbosity.get("enum") == ["minimal", "compact", "full"], (
        f"{tool_name}.verbosity must offer the Phase 1 levels, got {verbosity.get('enum')}"
    )
    # The MCP adapter defaults these three tools to 'minimal' for
    # context-constrained agents (the HTTP v1 surface keeps 'compact').
    assert verbosity.get("default") == "minimal"

    sql_profile = properties.get("sql_profile") or {}
    assert sql_profile.get("enum") == ["audit", "compact", "debug", "off"], (
        f"{tool_name}.sql_profile must include the new 'off' value, got {sql_profile.get('enum')}"
    )


def test_mcp_execute_tool_schema_advertises_row_format():
    from semantic_rails.mcp import MCP_TOOL_DEFINITIONS

    execute = next(row for row in MCP_TOOL_DEFINITIONS if row["name"] == "execute")
    row_format = execute["inputSchema"]["properties"].get("row_format") or {}

    assert row_format.get("enum") == ["records", "columns"]
    assert row_format.get("default") == "records"


def test_mcp_plan_tool_schema_advertises_query_detail():
    from semantic_rails.mcp import MCP_TOOL_DEFINITIONS

    plan = next(row for row in MCP_TOOL_DEFINITIONS if row["name"] == "plan")
    detail = plan["inputSchema"]["properties"].get("detail") or {}

    assert detail.get("enum") == ["query", "best", "full", "debug"]
    assert detail.get("default") == "best"


# ---------------------------------------------------------------------------
# Size budget — the headline Phase 1 win
# ---------------------------------------------------------------------------


def test_minimal_response_is_dramatically_smaller_than_full(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        minimal = runtime.validate({**_BASE_QUERY, "verbosity": "minimal"})
        full = runtime.validate({**_BASE_QUERY, "verbosity": "full"})

        minimal_size = len(json.dumps(minimal, default=str))
        full_size = len(json.dumps(full, default=str))

        # Audit cited 50–120 KB full responses; minimal must fit well
        # under the 5 KB target the plan calls out.
        assert minimal_size < 5_000, f"minimal validate must be <5KB, was {minimal_size}"
        # And dramatically smaller than full.
        assert minimal_size * 10 < full_size, (
            f"minimal ({minimal_size}) must be <10% of full ({full_size})"
        )
    finally:
        runtime.close()
