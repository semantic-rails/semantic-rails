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
  ``rows`` / ``row_count`` for execute, and the answer's ``window``, ``clock`` and
  ``observation_scope``) so an agent loop with a 25K
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

from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

_BASE_QUERY = {
    "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
    "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    "limit": 3,
}


_COMPACT_DROPPED_KEYS = (
    "physical_plan",
    "performance_plan",
    "semantic_summary",
    "compile_stats",
)


# What the answer means (its status, window, clock, observation scope, routes and
# assumptions, such as a governed metric filtering order_count) survives minimal too.
_TIME_MEANING_KEYS = frozenset(
    {"status", "window", "clock", "observation_scope", "routes", "assumptions"}
)
_MINIMAL_NONEXECUTE_KEYS = frozenset({"ok", "errors", "warnings", *_TIME_MEANING_KEYS})
_MINIMAL_COMPILE_KEYS = frozenset({"ok", "errors", "warnings", "rendered_sql", *_TIME_MEANING_KEYS})
_MINIMAL_EXECUTE_KEYS = frozenset(
    {"ok", "rows", "column_types", "row_count", "truncated", "errors", "warnings"}
    | _TIME_MEANING_KEYS
)


# ---------------------------------------------------------------------------
# Response fixtures and verbosity levels
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime(jaffle_snapshot):
    runtime = Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "method,keys,budget",
    [
        ("validate", _MINIMAL_NONEXECUTE_KEYS, 2_000),
        ("compile", _MINIMAL_COMPILE_KEYS, 6_000),
        ("query", _MINIMAL_EXECUTE_KEYS, None),
    ],
)
def test_minimal_response_keeps_its_product(runtime, method, keys, budget):
    result = getattr(runtime, method)(_BASE_QUERY, verbosity="minimal")
    assert set(result) <= keys
    assert result["ok"] is True
    if budget is not None:
        assert len(json.dumps(result, default=str)) < budget
    if method == "validate":
        assert "errors" not in result
        assert isinstance(result.get("warnings", []), list)
    elif method == "compile":
        assert isinstance(result["rendered_sql"], str) and result["rendered_sql"]
        assert "sql_plan" not in result and "logical_plan" not in result
    else:
        assert isinstance(result["rows"], list)
        assert result["row_count"] == len(result["rows"])
        assert "rendered_sql" not in result and "physical_plan" not in result


@pytest.mark.parametrize("method", ["validate", "compile", "query"])
def test_default_compact_response_drops_heavy_keys(runtime, method):
    result = getattr(runtime, method)(dict(_BASE_QUERY))
    for key in _COMPACT_DROPPED_KEYS:
        assert key not in result, key
    assert "output_columns" in result
    for column in result["output_columns"]:
        assert "lineage" not in column
    if method == "validate":
        assert result["ok"] is True and result["status"] == "ok"
        assert result.get("sql_profile") == "audit"
        assert "rendered_sql" not in result
        assert isinstance(result.get("explain", {}).get("rendered_sql"), str)
    else:
        assert "sql_plan" in result
        assert isinstance(result.get("rendered_sql"), str)
        assert "trace" in result and result["trace"]["selected"]["subjects"]
        assert "physical_plan" not in result["trace"]
        if method == "compile":
            assert result["rendered_sql"]
        else:
            assert isinstance(result["rows"], list)
            assert result["row_count"] == len(result["rows"])


@pytest.mark.parametrize("method", ["validate", "compile", "query"])
def test_full_response_keeps_plans_and_lineage(runtime, method):
    result = getattr(runtime, method)(_BASE_QUERY, verbosity="full")
    for key in ("compile_stats", "performance_plan", "semantic_summary"):
        assert key in result, key
    if method != "validate":
        assert "physical_plan" in result
    assert result["output_columns"]
    assert "lineage" in result["output_columns"][0]


@pytest.mark.parametrize(
    "method,options",
    [("validate", {}), ("compile", {}), ("compile", {"verbosity": "full"}), ("query", {})],
)
def test_sql_profile_off_keeps_the_non_sql_product(runtime, method, options):
    result = getattr(runtime, method)(_BASE_QUERY, sql_profile="off", **options)
    assert "rendered_sql" not in result and "sql_plan" not in result
    if options:
        assert "physical_plan" in result and "performance_plan" in result
    else:
        assert result.get("sql_profile") == "off"
        if method == "compile":
            assert result["status"] == "ok" and "normalized_query" in result
        elif method == "query":
            assert isinstance(result["rows"], list)


# ---------------------------------------------------------------------------
# MCP + HTTP plumbing
# ---------------------------------------------------------------------------


def test_mcp_execute_hoists_sql_profile_off(runtime):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

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


def test_mcp_execute_columns_row_format_preserves_values_and_counts(runtime):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    query = {**_BASE_QUERY, "order_by": [{"field": "period", "direction": "asc"}]}
    adapter = SemanticLayerMCPAdapter(runtime)
    records = adapter.call_tool("execute", {"query": dict(query)})
    columns = adapter.call_tool(
        "execute",
        {"query": dict(query), "row_format": "columns"},
    )

    assert records["ok"] is True
    assert columns["ok"] is True
    # Records are the default: a list of objects, with no row_format or columns echo.
    assert "row_format" not in records and "columns" not in records
    assert records["rows"] and isinstance(records["rows"][0], dict)
    assert records["row_count"] == len(records["rows"])
    assert columns["row_format"] == "columns"
    assert columns["row_count"] == records["row_count"]
    assert columns.get("warnings") == records.get("warnings")
    assert columns.get("errors") == records.get("errors")
    assert columns["columns"] == list(records["rows"][0].keys())
    assert columns["rows"] == [
        [record.get(column) for column in columns["columns"]] for record in records["rows"]
    ]


def test_mcp_execute_rejects_invalid_row_format(runtime):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    adapter = SemanticLayerMCPAdapter(runtime)
    envelope = adapter.call_tool(
        "execute",
        {"query": dict(_BASE_QUERY), "row_format": "tuples"},
    )

    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    assert envelope["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"
    assert envelope["errors"][0]["details"]["field"] == "row_format"


def test_mcp_execute_refuses_a_call_spread_beside_its_arguments(runtime):
    """The call travels as ``query``; its keys beside ``row_format`` are refused, never read."""
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    adapter = SemanticLayerMCPAdapter(runtime)
    envelope = adapter.call_tool("execute", {**_BASE_QUERY, "row_format": "columns"})

    assert envelope["ok"] is False
    (error,) = envelope["errors"]
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "select"
    (hint,) = error["recovery_hints"]
    assert '{"query":' in hint["message"]


def test_http_call_options_carry_verbosity_and_sql_profile():
    """A REST body sends its rendering options beside the call, under ``options``."""
    from semantic_rails.http_core import call_arguments

    body = {"query": {"select": []}, "options": {"verbosity": "minimal", "sql_profile": "off"}}
    call, options = call_arguments(body)
    assert call == {"select": []}
    assert options == {"verbosity": "minimal", "sql_profile": "off"}


def test_http_options_beside_the_call_are_refused_at_their_path():
    """Verbosity is never a key of the call, nor a sibling of ``query``."""
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.http_core import call_arguments

    with pytest.raises(SemanticLayerError) as exc:
        call_arguments({"query": {"select": []}, "verbosity": "minimal"})
    assert exc.value.details["path"] == "verbosity"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


def test_mcp_tool_schema_advertises_verbosity_and_sql_profile():
    from semantic_rails.mcp import MCP_TOOL_DEFINITIONS

    tool_name = "execute"
    matches = [row for row in MCP_TOOL_DEFINITIONS if row["name"] == tool_name]
    assert matches, f"tool {tool_name} not registered"
    schema = matches[0]["inputSchema"]
    properties = schema.get("properties", {})

    verbosity = properties.get("verbosity") or {}
    assert verbosity.get("enum") == ["minimal", "compact", "full"], (
        f"{tool_name}.verbosity must offer the Phase 1 levels, got {verbosity.get('enum')}"
    )
    # The MCP adapter defaults execute, in every mode, to 'minimal' for
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
    # The compact query is the default; 'best' and above are explicit opt-ins.
    assert detail.get("default") == "query"


# ---------------------------------------------------------------------------
# Size budget — the headline Phase 1 win
# ---------------------------------------------------------------------------


def test_minimal_response_is_dramatically_smaller_than_full(runtime):
    minimal = runtime.validate(_BASE_QUERY, verbosity="minimal")
    full = runtime.validate(_BASE_QUERY, verbosity="full")

    minimal_size = len(json.dumps(minimal, default=str))
    full_size = len(json.dumps(full, default=str))

    # Audit cited 50–120 KB full responses; minimal must fit well
    # under the 5 KB target the plan calls out.
    assert minimal_size < 5_000, f"minimal validate must be <5KB, was {minimal_size}"
    # And dramatically smaller than full.
    assert minimal_size * 10 < full_size, (
        f"minimal ({minimal_size}) must be <10% of full ({full_size})"
    )
