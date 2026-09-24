"""MCP segment tools offer an explicit minimal response.

validate, compile and execute already default to minimal responses over MCP,
but the three segment tools returned the runtime's whole response: logical,
SQL, physical and performance plans, most of them twice. segment-preview was
the largest default MCP response, over 11K tokens. Explicit
``verbosity="minimal"`` returns what each tool is for; omitted and ``"full"``
return the v1 whole response.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions

SEGMENT = "segment.jaffle.high_value_customers"
ARGUMENTS = {
    "segment-validate": {"segment_id": SEGMENT},
    "segment-explain": {"segment_id": SEGMENT},
    "segment-preview": {"segment_id": SEGMENT, "limit": 3},
}
TOOLS = tuple(ARGUMENTS)
PLANS = {"explain", "logical_plan", "sql_plan", "physical_plan", "performance_plan", "count_sql"}
ENVELOPE = {
    "ok",
    "status",
    "errors",
    "warnings",
    "recovery_hints",
    "api_version",
    "request_id",
    "package_id",
    "timing_ms",
    "request_context",
}
EXPECTED = {
    "segment-validate": {"segment", "normalized_segment", "derived_query"},
    "segment-explain": {"segment", "normalized_segment", "derived_query", "rendered_sql"},
    "segment-preview": {"segment", "rows", "preview_row_count", "member_count", "derived_query"},
}


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


@pytest.mark.parametrize("tool", TOOLS)
def test_segment_tools_advertise_v1_default_and_minimal_opt_in(tool: str) -> None:
    definition = next(item for item in list_tool_definitions() if item["name"] == tool)
    verbosity = definition["inputSchema"]["properties"]["verbosity"]
    assert verbosity["default"] == "full"
    assert verbosity["enum"] == ["minimal", "full"]


@pytest.mark.parametrize("tool", TOOLS)
def test_minimal_response_answers_without_compiler_plans(
    adapter: SemanticLayerMCPAdapter, tool: str
) -> None:
    response = adapter.call_tool(tool, {**ARGUMENTS[tool], "verbosity": "minimal"})
    assert response["ok"] is True, response["errors"]
    assert EXPECTED[tool] <= set(response), sorted(EXPECTED[tool] - set(response))
    assert not PLANS & set(response)
    extra = set(response) - ENVELOPE - EXPECTED[tool]
    assert extra <= {
        "segment_policy_effects",
        "policy_effects",
        "member_key_dimensions",
        "preview_dimensions",
    }, sorted(extra)
    assert response["segment"]["id"] == SEGMENT


@pytest.mark.parametrize("tool", TOOLS)
def test_full_response_on_request(adapter: SemanticLayerMCPAdapter, tool: str) -> None:
    full = adapter.call_tool(tool, {**ARGUMENTS[tool], "verbosity": "full"})
    slim = adapter.call_tool(tool, {**ARGUMENTS[tool], "verbosity": "minimal"})
    assert {"explain", "logical_plan"} <= set(full)
    default = adapter.call_tool(tool, ARGUMENTS[tool])
    assert set(default) == set(full)
    assert {"explain", "logical_plan"} <= set(default)
    # "compact", the whole-response level on other tools, means the same here.
    compact = adapter.call_tool(tool, {**ARGUMENTS[tool], "verbosity": "compact"})
    assert set(compact) == set(full)
    assert len(str(slim)) < len(str(full)) / 3


def test_preview_keeps_its_rows_and_counts(adapter: SemanticLayerMCPAdapter) -> None:
    arguments = ARGUMENTS["segment-preview"]
    full = adapter.call_tool("segment-preview", {**arguments, "verbosity": "full"})
    slim = adapter.call_tool("segment-preview", {**arguments, "verbosity": "minimal"})
    for key in ("preview_row_count", "member_count", "derived_query"):
        assert slim[key] == full[key], key
    # The sample itself is unordered, so compare its shape.
    assert len(slim["rows"]) == len(full["rows"]) == 3
    assert {tuple(sorted(row)) for row in slim["rows"]} == {
        tuple(sorted(row)) for row in full["rows"]
    }


def test_a_failed_segment_call_keeps_its_error(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("segment-validate", {"segment_id": "segment.jaffle.nope"})
    assert response["ok"] is False
    assert response["errors"] and response["errors"][0]["code"]
