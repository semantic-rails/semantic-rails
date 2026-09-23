"""MCP execute caps rows by default and says so when it does.

An agent's plausible mistake, a time window with no grain, used to return one
row per order timestamp: hundreds of thousands of tokens in a single result.
Execute now returns at most ``max_rows`` rows (default 200). A truncated
result carries ``truncated``, ``total_row_count`` and a warning that says how
to narrow the query.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import (
    MCP_DEFAULT_MAX_ROWS,
    MCP_ROW_COUNT_CEILING,
    SemanticLayerMCPAdapter,
    list_tool_definitions,
)

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
REVENUE = [{"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}]
# 365 daily buckets: more than the default cap, fewer than the counting ceiling.
DAILY_REVENUE = {
    "version": 2,
    "select": REVENUE,
    "time": {"temporal_role": ORDER_TIME, "grain": "day"},
}
# A window with no grain, grouped by store: one row per store and order timestamp.
NO_GRAIN_WINDOW = {
    "version": 2,
    "select": REVENUE,
    "group_by": [STORE],
    "time": {"temporal_role": ORDER_TIME, "start": "2017-04-01", "end": "2017-07-01"},
}


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


def _codes(response: dict[str, Any]) -> list[str]:
    return [str(item.get("code")) for item in response.get("warnings") or []]


def _truncation(response: dict[str, Any]) -> dict[str, Any]:
    return next(w for w in response["warnings"] if w["code"] == "EXECUTE_ROWS_TRUNCATED")


def test_execute_advertises_the_default_cap() -> None:
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    max_rows = execute["inputSchema"]["properties"]["max_rows"]
    assert max_rows["default"] == MCP_DEFAULT_MAX_ROWS == 200


def test_a_large_result_is_capped_with_its_exact_total(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE})
    assert response["ok"], response["errors"]
    assert response["row_count"] == len(response["rows"]) == MCP_DEFAULT_MAX_ROWS
    assert response["truncated"] is True
    assert response["total_row_count"] == 365
    warning = _truncation(response)
    assert "Returned 200 of 365 rows" in warning["message"]
    assert warning["details"] == {"returned_rows": 200, "total_row_count": 365, "max_rows": 200}


def test_a_window_without_a_grain_is_capped_and_flagged(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": NO_GRAIN_WINDOW})
    assert response["ok"], response["errors"]
    assert response["row_count"] == MCP_DEFAULT_MAX_ROWS
    assert response["truncated"] is True
    # More rows exist than execute reads, so the total is unknown but bounded.
    assert response["total_row_count"] is None
    assert f"more than {MCP_ROW_COUNT_CEILING:,}" in _truncation(response)["message"]
    assert "time.grain" in _truncation(response)["message"]
    assert "UNGRAINED_TIME_PROJECTION" in _codes(response)


def test_max_rows_raises_the_cap(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 400})
    assert response["row_count"] == 365
    assert response["truncated"] is False
    assert "total_row_count" not in response
    assert "EXECUTE_ROWS_TRUNCATED" not in _codes(response)


def test_the_querys_own_row_limit_is_a_fence(adapter: SemanticLayerMCPAdapter) -> None:
    fenced = {**DAILY_REVENUE, "limits": {"max_rows": 50}}
    alone = adapter.call_tool("execute", {"query": fenced})
    assert alone["row_count"] == 50
    assert alone["truncated"] is True
    assert alone["total_row_count"] is None
    raised = adapter.call_tool("execute", {"query": fenced, "max_rows": 300})
    assert raised["row_count"] == 50
    lowered = adapter.call_tool("execute", {"query": fenced, "max_rows": 10})
    assert lowered["row_count"] == 10


@pytest.mark.parametrize("bad", ["many", 0, True])
def test_max_rows_must_be_a_positive_integer(adapter: SemanticLayerMCPAdapter, bad: Any) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": bad})
    assert response["ok"] is False
    assert response["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"


def test_small_results_are_untouched(adapter: SemanticLayerMCPAdapter) -> None:
    query = {**DAILY_REVENUE, "time": {"temporal_role": ORDER_TIME, "grain": "month"}}
    response = adapter.call_tool("execute", {"query": query})
    assert response["row_count"] == 12
    assert response["truncated"] is False
    assert "total_row_count" not in response
    assert "EXECUTE_ROWS_TRUNCATED" not in _codes(response)


def test_columnar_results_are_capped_too(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "row_format": "columns"})
    assert response["row_format"] == "columns"
    assert len(response["rows"]) == response["row_count"] == MCP_DEFAULT_MAX_ROWS
    assert response["total_row_count"] == 365


@pytest.mark.parametrize("tool", ["validate", "compile"])
def test_a_grouped_window_without_a_grain_is_flagged_before_execution(
    adapter: SemanticLayerMCPAdapter, tool: str
) -> None:
    response = adapter.call_tool(tool, {"query": NO_GRAIN_WINDOW})
    assert response["ok"], response["errors"]
    assert _codes(response).count("UNGRAINED_TIME_PROJECTION") == 1


def test_an_ungrouped_window_without_a_grain_is_flagged_once(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    ungrouped = {key: value for key, value in NO_GRAIN_WINDOW.items() if key != "group_by"}
    response = adapter.call_tool("validate", {"query": ungrouped})
    # The runtime already warns for ungrouped queries; the adapter doesn't repeat it.
    assert _codes(response).count("UNGRAINED_TIME_PROJECTION") == 1
