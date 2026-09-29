"""A time window with no grain returns one total; an oversized result is refused.

Gold values come from raw SQL against the seeded tables, not from the compiler.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.compiler import plan_query
from semantic_rails.mcp import (
    MCP_DEFAULT_MAX_RESULT_CHARS,
    SemanticLayerMCPAdapter,
)
from semantic_rails.runtime import Runtime

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
REVENUE = {"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}
ORDERS = {"as": "orders", "expression": {"measure": "measure.jaffle.order_count"}}
LIMIT_ENV = "SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS"


@pytest.fixture()
def runtime(runtime_factory: Any) -> Iterator[Runtime]:
    rt = runtime_factory("jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


def _query(select: list[dict[str, Any]], time: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"version": 2, "select": select, "time": {"temporal_role": ORDER_TIME, **time}, **extra}


def _gold(runtime: Runtime, sql: str) -> list[dict[str, Any]]:
    """Run raw SQL on the runtime's own database, bypassing the compiler."""
    return list(runtime._get_adapter().query(sql))


def _codes(response: dict[str, Any]) -> list[str]:
    return [str(item.get("code")) for item in response.get("warnings") or []]


def test_a_window_without_a_grain_is_one_total(runtime: Runtime) -> None:
    response = runtime.query(
        _query([REVENUE, ORDERS], {"start": "2017-04-01", "end": "2017-07-01"})
    )
    gold = _gold(
        runtime,
        "SELECT COUNT(*) AS orders, SUM(order_total_cents) / 100.0 AS revenue FROM jaffle_order "
        "WHERE ordered_at >= TIMESTAMP '2017-04-01' AND ordered_at < TIMESTAMP '2017-07-01'",
    )
    assert response["row_count"] == 1
    row = response["rows"][0]
    assert set(row) == {"revenue", "orders"}  # no time column
    assert row["orders"] == gold[0]["orders"]
    assert row["revenue"] == pytest.approx(gold[0]["revenue"])
    assert [column["field"] for column in response["output_columns"]] == ["revenue", "orders"]
    assert len(response["assumptions"]) == 1 and "one total" in response["assumptions"][0]
    assert "UNGRAINED_TIME_PROJECTION" not in _codes(response)


def test_a_window_inside_one_day_is_one_total(runtime: Runtime) -> None:
    response = runtime.query(
        _query([REVENUE, ORDERS], {"start": "2017-04-03T12:00:00", "end": "2017-04-03T13:00:00"})
    )
    gold = _gold(
        runtime,
        "SELECT COUNT(*) AS orders, SUM(order_total_cents) / 100.0 AS revenue FROM jaffle_order "
        "WHERE ordered_at >= TIMESTAMP '2017-04-03 12:00:00' "
        "AND ordered_at < TIMESTAMP '2017-04-03 13:00:00'",
    )
    assert gold[0]["orders"] > 1  # a raw-timestamp answer would have been several rows
    assert response["row_count"] == 1
    assert response["rows"][0]["orders"] == gold[0]["orders"]
    assert response["rows"][0]["revenue"] == pytest.approx(gold[0]["revenue"])


def test_a_grouped_window_is_one_total_per_group(runtime: Runtime) -> None:
    response = runtime.query(
        _query(
            [REVENUE],
            {"start": "2017-04-01", "end": "2017-07-01"},
            group_by=[STORE],
            order_by=[{"field": "time"}, {"field": STORE}],
        )
    )
    gold = _gold(
        runtime,
        "SELECT s.store_name AS store, SUM(o.order_total_cents) / 100.0 AS revenue "
        "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
        "WHERE o.ordered_at >= TIMESTAMP '2017-04-01' AND o.ordered_at < TIMESTAMP '2017-07-01' "
        "GROUP BY 1",
    )
    got = {row[STORE]: row["revenue"] for row in response["rows"]}
    assert got == pytest.approx({row["store"]: row["revenue"] for row in gold})
    assert all(set(row) == {STORE, "revenue"} for row in response["rows"])


def test_a_grain_still_returns_one_row_per_period(runtime: Runtime) -> None:
    response = runtime.query(
        _query([REVENUE], {"grain": "month", "start": "2017-04-01", "end": "2017-07-01"})
    )
    gold = _gold(
        runtime,
        "SELECT DATE_TRUNC('month', ordered_at) AS month, SUM(order_total_cents) / 100.0 AS revenue "
        "FROM jaffle_order WHERE ordered_at >= TIMESTAMP '2017-04-01' "
        "AND ordered_at < TIMESTAMP '2017-07-01' GROUP BY 1",
    )
    key = f"{ORDER_TIME}__month"
    assert {row[key]: row["revenue"] for row in response["rows"]} == pytest.approx(
        {row["month"]: row["revenue"] for row in gold}
    )
    assert response["assumptions"] == []


@pytest.mark.parametrize(
    "time",
    [{}, {"start": "2017-04-01"}, {"end": "2017-04-01"}],
    ids=["no bounds", "start only", "end only"],
)
def test_only_a_bounded_window_collapses(runtime: Runtime, time: dict[str, Any]) -> None:
    response = runtime.query(_query([REVENUE], time))
    collapses = bool(time)
    assert (response["row_count"] == 1) is collapses
    assert (ORDER_TIME not in response["rows"][0]) is collapses
    assert ("UNGRAINED_TIME_PROJECTION" in _codes(response)) is not collapses


def test_an_expression_that_needs_the_time_axis_is_not_collapsed(runtime: Runtime) -> None:
    def collapses(select: dict[str, Any]) -> bool:
        payload = _query([select], {"end": "2017-04-04"})
        return bool(plan_query(runtime._config, None, payload).time.get("window_total"))

    rolling = {"expression": {"metric": "metric.sales.rolling_7d_revenue_direct"}, "as": "r"}
    assert collapses(REVENUE)
    assert not collapses(rolling)


def test_the_default_cap_is_32k_characters() -> None:
    assert MCP_DEFAULT_MAX_RESULT_CHARS == 32_000


@pytest.fixture()
def mcp(runtime: Runtime) -> SemanticLayerMCPAdapter:
    return SemanticLayerMCPAdapter(runtime)


def test_execute_reports_the_total_and_its_assumption(mcp: SemanticLayerMCPAdapter) -> None:
    query = _query([REVENUE], {"start": "2017-04-01", "end": "2017-07-01"}, group_by=[STORE])
    response = mcp.call_tool("execute", {"query": query})
    assert response["ok"], response["errors"]
    assert response["row_count"] == 2 and response["truncated"] is False
    # The default response is minimal, and an assumption changes what the numbers mean.
    assert "one total" in response["assumptions"][0]
    assert "UNGRAINED_GROUPED_TIME_PROJECTION" not in _codes(response)
    assert "assumptions" not in mcp.call_tool(
        "execute", {"query": {**query, "time": {"temporal_role": ORDER_TIME, "grain": "year"}}}
    )


def test_an_oversized_result_is_refused_with_its_row_count(
    mcp: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    daily = _query([REVENUE], {"grain": "day"})
    fits = mcp.call_tool("execute", {"query": daily, "max_rows": 5})
    assert fits["ok"] and fits["row_count"] == 5

    monkeypatch.setenv(LIMIT_ENV, "2000")
    response = mcp.call_tool("execute", {"query": daily, "max_rows": 400})
    assert response["ok"] is False
    assert "rows" not in response
    error = response["errors"][0]
    assert error["code"] == "RESULT_TOO_LARGE"
    assert "365 rows" in error["message"]
    assert "coarser time.grain" in error["message"]
    details = error["details"]
    assert details["total_row_count"] == 365 and details["row_count"] == 365
    assert details["max_result_chars"] == 2000 and details["result_chars"] > 2000


def test_a_capped_result_that_is_still_too_large_names_the_full_count(
    mcp: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LIMIT_ENV, "2000")
    role_only = _query([REVENUE], {}, group_by=[STORE])
    response = mcp.call_tool("execute", {"query": role_only})
    error = response["errors"][0]
    assert error["code"] == "RESULT_TOO_LARGE"
    assert "more than 10,000 rows" in error["message"]
    assert "time.grain" in error["message"]


@pytest.mark.parametrize("bad", ["", "0", "-5", "many"])
def test_a_bad_limit_falls_back_to_the_default(
    mcp: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv(LIMIT_ENV, bad)
    query = _query([REVENUE], {"grain": "month"})
    assert mcp.call_tool("execute", {"query": query})["row_count"] == 12
