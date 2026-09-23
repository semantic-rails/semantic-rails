"""plan says when its draft doesn't honor part of the question.

A draft that validates can still drop a time window, a ranking, a filter value
or a qualifier and report ``status: ok``. Structural gaps downgrade the plan to
``low_confidence`` with a ``PLAN_INTENT_COVERAGE_GAP`` reason; question words
the draft uses nowhere come back as a ``PLAN_UNMATCHED_TERMS`` warning.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner.faithfulness import (
    _ranking_request,
    intent_faithfulness_why,
    unmatched_intent_terms,
)
from semantic_rails.planner.intent_ir import parse_intent

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
PRODUCT = "dimension.jaffle_item_product_name"
REVENUE = {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
ITEM_REVENUE = {
    "as": "item_revenue_usd",
    "expression": {"measure": "measure.jaffle.item_revenue_usd"},
}


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


@pytest.mark.parametrize(
    ("text", "limit", "direction", "noun"),
    [
        ("top 5 products by revenue in 2017", 5, "DESC", "products"),
        ("the 3 lowest-selling products by item revenue", 3, "ASC", "products"),
        ("the 5 customers who spent the most", 5, "DESC", "customers"),
        # "which store" asks for one; "which segments" for all of them.
        ("which store had the most orders in 2017", 1, "DESC", "store"),
        ("Which customer segments drove the most historical revenue?", None, "DESC", "segments"),
        ("which product type sold the most", 1, "DESC", "type"),
        ("which 3 stores have the highest revenue", 3, "DESC", "stores"),
        ("the 3 highest-revenue days in August 2017", 3, "DESC", "days"),
        ("bottom 2 stores by orders", 2, "ASC", "stores"),
    ],
)
def test_ranking_requests_parse(text: str, limit: int | None, direction: str, noun: str) -> None:
    request = _ranking_request(text)
    assert request is not None
    assert (request["limit"], request["direction"], request["noun"]) == (limit, direction, noun)


@pytest.mark.parametrize(
    "text",
    [
        "orders by store",
        "revenue in 2017",
        "orders at 3 stores",
        # Thresholds are not rankings.
        "monthly revenue from customers with at least 10 orders by store",
        "orders from customers with at most 3 visits",
        "most recent month revenue",
    ],
)
def test_non_rankings_are_not_rankings(text: str) -> None:
    assert _ranking_request(text) is None


def _gap_kinds(adapter: SemanticLayerMCPAdapter, text: str, query: dict[str, Any]) -> list[str]:
    runtime = adapter.runtime
    why = intent_faithfulness_why(
        runtime, question=text, intent_ir=parse_intent(runtime, text), query=query
    )
    return [gap["kind"] for gap in (why or {}).get("details", {}).get("gaps", [])]


def test_a_dropped_window_is_a_gap(adapter: SemanticLayerMCPAdapter) -> None:
    unbounded = {"version": 2, "select": [REVENUE]}
    assert _gap_kinds(adapter, "total revenue in 2017", unbounded) == ["time_window_unrealized"]
    bounded = {
        **unbounded,
        "time": {
            "temporal_role": ORDER_TIME,
            "grain": "year",
            "start": "2017-01-01",
            "end": "2018-01-01",
        },
    }
    assert _gap_kinds(adapter, "total revenue in 2017", bounded) == []


def test_a_dropped_ranking_is_a_gap(adapter: SemanticLayerMCPAdapter) -> None:
    text = "the 3 lowest-selling products by item revenue"
    ranked = {
        "version": 2,
        "select": [ITEM_REVENUE],
        "group_by": [PRODUCT],
        "order_by": [{"field": "item_revenue_usd", "direction": "ASC"}],
        "limit": 3,
    }
    assert _gap_kinds(adapter, text, ranked) == []
    for broken in (
        {**ranked, "limit": None},
        {**ranked, "order_by": [{"field": "item_revenue_usd", "direction": "DESC"}]},
        {**ranked, "group_by": [STORE]},
    ):
        assert _gap_kinds(adapter, text, broken) == ["ranking_unrealized"]


def test_every_named_value_must_reach_a_filter(adapter: SemanticLayerMCPAdapter) -> None:
    text = "revenue by store for Philadelphia and Brooklyn"
    both = {
        "version": 2,
        "select": [REVENUE],
        "group_by": [STORE],
        "where": [{"field": STORE, "op": "in", "value": ["Philadelphia", "Brooklyn"]}],
    }
    assert _gap_kinds(adapter, text, both) == []
    one = {**both, "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}]}
    assert _gap_kinds(adapter, text, one) == ["filter_values_unrealized"]


def test_unmatched_terms(adapter: SemanticLayerMCPAdapter) -> None:
    revenue = {"version": 2, "select": [REVENUE]}
    runtime = adapter.runtime
    assert unmatched_intent_terms(runtime, "cumulative revenue by month", revenue) == ["cumulative"]
    assert unmatched_intent_terms(
        runtime, "what's the weather in Philadelphia tomorrow", revenue
    ) == [
        "weather",
        "philadelphia",
        "tomorrow",
    ]
    # Framing words, plurals and near-miss spellings are accounted for.
    assert (
        unmatched_intent_terms(runtime, "show me the total revnue by month for 2017", revenue) == []
    )


def test_mcp_plan_reports_what_it_could_not_honor(adapter: SemanticLayerMCPAdapter) -> None:
    ranked = adapter.call_tool("plan", {"intent": "top 5 products by revenue in 2017"})
    # Today's draft drops the product dimension, so it must not report ok.
    assert ranked["status"] == "low_confidence"
    assert ranked["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    weather = adapter.call_tool("plan", {"intent": "what's the weather in Philadelphia tomorrow"})
    codes = [warning["code"] for warning in weather["warnings"]]
    assert "PLAN_UNMATCHED_TERMS" in codes
    clean = adapter.call_tool("plan", {"intent": "total revenue in 2017"})
    assert clean["status"] == "ok"
    assert clean["warnings"] == []


def test_mcp_plan_defaults_to_the_query_detail(adapter: SemanticLayerMCPAdapter) -> None:
    default = adapter.call_tool("plan", {"intent": "revenue by store"})
    assert default["best"]["query_ir"]
    for key in ("intent_ir", "next"):
        assert key not in default
    assert "trace" not in default["best"]
    detailed = adapter.call_tool("plan", {"intent": "revenue by store", "detail": "best"})
    assert "intent_ir" in detailed and "trace" in detailed["best"]
