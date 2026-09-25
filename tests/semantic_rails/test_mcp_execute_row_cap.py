"""MCP execute caps rows and says so when it does.

An agent's plausible mistake, a time window with no grain, used to return one
row per order timestamp: hundreds of thousands of tokens in a single result.
Execute returns at most ``max_rows`` rows (default 200). A truncated
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


def test_execute_advertises_its_default_cap() -> None:
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    max_rows = execute["inputSchema"]["properties"]["max_rows"]
    assert max_rows["default"] == MCP_DEFAULT_MAX_ROWS
    assert max_rows["maximum"] == 100_000


def test_a_large_result_is_capped_with_its_exact_total(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 200})
    assert response["ok"], response["errors"]
    assert response["row_count"] == len(response["rows"]) == MCP_DEFAULT_MAX_ROWS
    assert response["truncated"] is True
    assert response["total_row_count"] == 365
    warning = _truncation(response)
    assert "Returned 200 of 365 rows" in warning["message"]
    assert warning["details"] == {"returned_rows": 200, "total_row_count": 365, "max_rows": 200}


def test_a_window_without_a_grain_is_capped_and_flagged(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": NO_GRAIN_WINDOW, "max_rows": 200})
    assert response["ok"], response["errors"]
    assert response["row_count"] == MCP_DEFAULT_MAX_ROWS
    assert response["truncated"] is True
    # More rows exist than execute reads, so the total is unknown but bounded.
    assert response["total_row_count"] is None
    assert f"more than {MCP_ROW_COUNT_CEILING:,}" in _truncation(response)["message"]
    assert "time.grain" in _truncation(response)["message"]
    assert "UNGRAINED_GROUPED_TIME_PROJECTION" in _codes(response)


def test_max_rows_raises_the_cap(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 400})
    assert response["row_count"] == 365
    assert response["truncated"] is False
    assert "total_row_count" not in response
    assert "EXECUTE_ROWS_TRUNCATED" not in _codes(response)


def test_the_querys_own_row_limit_only_lowers_the_cap(adapter: SemanticLayerMCPAdapter) -> None:
    fenced = {**DAILY_REVENUE, "limits": {"max_rows": 50}}
    alone = adapter.call_tool("execute", {"query": fenced, "max_rows": 200})
    assert alone["row_count"] == 50
    assert alone["truncated"] is True
    assert alone["total_row_count"] is None
    # The query's limit, not max_rows, is what binds here, so don't suggest raising max_rows.
    assert "limits.max_rows caps the rows fetched" in _truncation(alone)["message"]
    raised = adapter.call_tool("execute", {"query": fenced, "max_rows": 300})
    assert raised["row_count"] == 50
    lowered = adapter.call_tool("execute", {"query": fenced, "max_rows": 10})
    assert lowered["row_count"] == 10
    # An operator's ceiling above the default is not a response size.
    ceiling = {**DAILY_REVENUE, "limits": {"max_rows": 1000}}
    assert adapter.call_tool("execute", {"query": ceiling})["row_count"] == MCP_DEFAULT_MAX_ROWS
    assert adapter.call_tool("execute", {"query": ceiling, "max_rows": 200})["row_count"] == 200
    assert adapter.call_tool("execute", {"query": ceiling, "max_rows": 400})["row_count"] == 365


def test_the_explicit_cap_is_transport_only(adapter: SemanticLayerMCPAdapter) -> None:
    first = adapter.call_tool(
        "execute", {"query": DAILY_REVENUE, "verbosity": "compact", "max_rows": 200}
    )
    assert first["row_count"] == MCP_DEFAULT_MAX_ROWS
    # The echo is the caller's query, without the fetch ceiling execute added.
    assert "limits" not in first["query"]
    again = adapter.call_tool("execute", {"query": first["query"], "max_rows": 400})
    assert again["row_count"] == 365
    fenced = {**DAILY_REVENUE, "limits": {"max_rows": 5000}}
    echoed = adapter.call_tool(
        "execute", {"query": fenced, "verbosity": "compact", "max_rows": 200}
    )["query"]
    assert echoed["limits"] == {"max_rows": 5000}


@pytest.mark.parametrize("bad", ["many", "²", "٣", 0, -1, True, 1.5, float("inf"), 1e30, 100_001])
def test_max_rows_must_be_a_whole_number_in_range(
    adapter: SemanticLayerMCPAdapter, bad: Any
) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": bad})
    assert response["ok"] is False
    assert response["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"


@pytest.mark.parametrize("good", [5, "5", 5.0])
def test_max_rows_accepts_whole_numbers(adapter: SemanticLayerMCPAdapter, good: Any) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": good})
    assert response["row_count"] == 5


def test_small_results_are_untouched(adapter: SemanticLayerMCPAdapter) -> None:
    query = {**DAILY_REVENUE, "time": {"temporal_role": ORDER_TIME, "grain": "month"}}
    response = adapter.call_tool("execute", {"query": query})
    assert response["row_count"] == 12
    assert response["truncated"] is False
    assert "total_row_count" not in response
    assert "EXECUTE_ROWS_TRUNCATED" not in _codes(response)


def test_columnar_results_are_capped_too(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool(
        "execute", {"query": DAILY_REVENUE, "row_format": "columns", "max_rows": 200}
    )
    assert response["row_format"] == "columns"
    assert len(response["rows"]) == response["row_count"] == MCP_DEFAULT_MAX_ROWS
    assert response["total_row_count"] == 365


@pytest.mark.parametrize("mode", ["validate", "sql"])
def test_a_grouped_window_without_a_grain_is_flagged_before_execution(
    adapter: SemanticLayerMCPAdapter, mode: str
) -> None:
    response = adapter.call_tool("execute", {"query": NO_GRAIN_WINDOW, "mode": mode})
    assert response["ok"], response["errors"]
    assert _codes(response).count("UNGRAINED_GROUPED_TIME_PROJECTION") == 1
    warning = next(
        w for w in response["warnings"] if w["code"] == "UNGRAINED_GROUPED_TIME_PROJECTION"
    )
    # Same shape as the runtime's UNGRAINED_TIME_PROJECTION, which covers ungrouped queries.
    assert warning["details"]["temporal_role"] == ORDER_TIME
    assert warning["details"]["recovery_hints"][0]["code"] == "SET_TIME_GRAIN"


def test_an_ungrouped_window_without_a_grain_is_flagged_once(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    ungrouped = {key: value for key, value in NO_GRAIN_WINDOW.items() if key != "group_by"}
    response = adapter.call_tool("execute", {"query": ungrouped, "mode": "validate"})
    # The runtime warns for ungrouped queries; the adapter adds nothing.
    assert _codes(response).count("UNGRAINED_TIME_PROJECTION") == 1
    assert "UNGRAINED_GROUPED_TIME_PROJECTION" not in _codes(response)


def test_a_grouped_role_without_a_window_or_grain_is_flagged(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    role_only = {**NO_GRAIN_WINDOW, "time": {"temporal_role": ORDER_TIME}}
    response = adapter.call_tool("execute", {"query": role_only, "max_rows": 200})
    assert "UNGRAINED_GROUPED_TIME_PROJECTION" in _codes(response)
    assert "time.grain" in _truncation(response)["message"]


def test_running_totals_do_not_hide_the_missing_grain() -> None:
    from semantic_rails.mcp import _grouped_ungrained_time_warning

    def query(kind: str) -> dict[str, Any]:
        expression = {"kind": kind, "input": {"measure": "measure.jaffle.revenue_usd"}}
        return {**NO_GRAIN_WINDOW, "select": [{"as": "v", "expression": expression}]}

    # A running total's buckets come from time.grain, so its absence still matters...
    assert _grouped_ungrained_time_warning(query("cumulative")) is not None
    assert _grouped_ungrained_time_warning(query("period_to_date")) is not None
    # ...while kinds on their own clock don't project the raw timestamp.
    assert _grouped_ungrained_time_warning(query("rolling")) is None
