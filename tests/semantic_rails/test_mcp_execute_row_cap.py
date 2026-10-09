"""MCP execute caps rows and says so when it does.

A list of rows (``group_by`` with a window and no grain) returns one row per store and
order timestamp: hundreds of thousands of tokens in a single result. Execute returns at
most ``max_rows`` rows (default 200) and fetches no more than that: the warehouse adapter
reads one row past the cap to tell. A truncated result carries ``truncated``, a null
``total_row_count`` (the rest is not counted) and a warning that says how to narrow the
query. (A bounded window with no grain over measures is a single total; see
``test_window_total.py``.)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import (
    MCP_DEFAULT_MAX_ROWS,
    SemanticLayerMCPAdapter,
    list_tool_definitions,
)

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
REVENUE = ["measure.jaffle.revenue_usd"]
# 365 daily buckets: more than the default cap.
DAILY_REVENUE = {"select": REVENUE, "time": {"temporal_role": ORDER_TIME, "grain": "day"}}
# Stores listed over a window with no grain: one row per store, never one per timestamp.
NO_GRAIN_WINDOW = {
    "group_by": [STORE],
    "time": {"temporal_role": ORDER_TIME, "start": "2016-01-01"},
}


@pytest.fixture()
def adapter(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SemanticLayerMCPAdapter]:
    # These tests are about the row cap; the character cap has its own tests.
    monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", "10000000")
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


def test_a_large_result_is_capped_and_says_more_exist(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 200})
    assert response["ok"], response["errors"]
    assert response["row_count"] == len(response["rows"]) == MCP_DEFAULT_MAX_ROWS
    assert response["truncated"] is True
    assert response["total_row_count"] is None
    warning = _truncation(response)
    assert "Returned the first 200 rows; more exist" in warning["message"]
    assert warning["details"] == {"returned_rows": 200, "total_row_count": None, "max_rows": 200}


def test_execute_fetches_only_its_cap(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = adapter.runtime.query
    fetched: list[Any] = []

    def run(call: Any, **options: Any) -> Any:
        fetched.append(options["limits"])
        return query(call, **options)

    monkeypatch.setattr(adapter.runtime, "query", run)
    adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 7})
    assert fetched == [{"max_rows": 7}]


def test_a_window_without_a_grain_lists_each_group_once(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": NO_GRAIN_WINDOW, "max_rows": 200})
    assert response["ok"], response["errors"]
    assert sorted(row[STORE] for row in response["rows"]) == ["Brooklyn", "Philadelphia"]
    assert response["truncated"] is False
    assert response["time_shape"] == "window_total"


def test_max_rows_raises_the_cap(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 400})
    assert response["row_count"] == 365
    assert response["truncated"] is False
    assert "total_row_count" not in response
    assert "EXECUTE_ROWS_TRUNCATED" not in _codes(response)


def test_the_questions_top_n_is_not_the_transport_cap(adapter: SemanticLayerMCPAdapter) -> None:
    top = {**DAILY_REVENUE, "limit": 50}
    alone = adapter.call_tool("execute", {"query": top, "max_rows": 200})
    assert alone["row_count"] == 50 and alone["truncated"] is False
    lowered = adapter.call_tool("execute", {"query": top, "max_rows": 10})
    assert lowered["row_count"] == 10 and lowered["truncated"] is True


def test_the_cap_is_an_argument_not_part_of_the_call(adapter: SemanticLayerMCPAdapter) -> None:
    first = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 200})
    assert first["row_count"] == MCP_DEFAULT_MAX_ROWS
    again = adapter.call_tool("execute", {"query": DAILY_REVENUE, "max_rows": 400})
    assert again["row_count"] == 365
    fenced = adapter.call_tool("execute", {"query": {**DAILY_REVENUE, "limits": {"max_rows": 5}}})
    assert fenced["errors"][0]["details"]["path"] == "limits"


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
    assert response["total_row_count"] is None


def test_a_role_without_a_window_or_grain_is_refused(adapter: SemanticLayerMCPAdapter) -> None:
    role_only = {**NO_GRAIN_WINDOW, "time": {"temporal_role": ORDER_TIME}}
    response = adapter.call_tool("execute", {"query": role_only, "max_rows": 200})
    assert response["ok"] is False
    assert response["errors"][0]["details"]["path"] == "time"
