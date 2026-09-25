"""plan says when its draft doesn't honor part of the question.

A draft that validates can still drop a time window, a ranking, a filter value
or a qualifier and report ``status: ok``. Structural gaps downgrade the plan to
``low_confidence`` with a ``PLAN_INTENT_COVERAGE_GAP`` reason; question words
the draft uses nowhere come back as a ``PLAN_UNMATCHED_TERMS`` warning.

Every check here runs on hand-built Query IR, correct and broken, so none of
them depends on what today's planner happens to draft.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner._base import _named_metric
from semantic_rails.planner.faithfulness import (
    _filter_value_gaps,
    _ranking_request,
    intent_faithfulness_why,
    unmatched_intent_terms,
)
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
PRODUCT = "dimension.jaffle_item_product_name"
PRODUCT_TYPE = "dimension.jaffle_item_product_type"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
REVENUE = {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
ORDERS = {"as": "order_count", "expression": {"measure": "measure.jaffle.order_count"}}
ITEM_REVENUE = {
    "as": "item_revenue_usd",
    "expression": {"measure": "measure.jaffle.item_revenue_usd"},
}
YEAR_2017 = {"start": "2017-01-01", "end": "2018-01-01"}


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


def _query(select: dict[str, Any] = REVENUE, **parts: Any) -> dict[str, Any]:
    return {"version": 2, "select": [select], **parts}


def _gaps(adapter: SemanticLayerMCPAdapter, text: str, query: dict[str, Any], **kwargs: Any):
    runtime = adapter.runtime
    why = intent_faithfulness_why(
        runtime, question=text, intent_ir=parse_intent(runtime, text), query=query, **kwargs
    )
    return (why or {}).get("details", {}).get("gaps", [])


def _gap_kinds(adapter: SemanticLayerMCPAdapter, text: str, query: dict[str, Any], **kwargs: Any):
    return [gap["kind"] for gap in _gaps(adapter, text, query, **kwargs)]


# --- ranking requests ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit", "direction", "noun"),
    [
        ("top 5 products by revenue in 2017", 5, "DESC", "products"),
        ("top five products by revenue", 5, "DESC", "products"),
        ("bottom 2 stores by orders", 2, "ASC", "stores"),
        # A number after top/bottom is a count even when it looks like a year.
        ("top 2000 customers by lifetime spend", 2000, "DESC", "customers"),
        ("the top store by revenue", 1, "DESC", "store"),
        ("what are the top products", None, "DESC", "products"),
        ("the 3 lowest-selling products by item revenue", 3, "ASC", "products"),
        ("the 3 highest-revenue days in August 2017", 3, "DESC", "days"),
        ("5 best-selling products", 5, "DESC", "products"),
        ("best-selling products", None, "DESC", "products"),
        ("the best-selling product", 1, "DESC", "product"),
        # The order comes from the ranking's own clause...
        ("the 5 customers who spent the most", 5, "DESC", "customers"),
        ("the 3 stores with the least revenue", 3, "ASC", "stores"),
        ("top 5 products by revenue excluding the worst store", 5, "DESC", "products"),
        ("top 3 products by revenue at the store with the fewest orders", 3, "DESC", "products"),
        # ...and "at least", across any spacing, is a threshold.
        (
            "the 2 stores with the most revenue among those with at\nleast 10 orders",
            2,
            "DESC",
            "stores",
        ),
        # "which store" asks for one; "which segments" for all of them.
        ("which store had the most orders in 2017", 1, "DESC", "store"),
        ("Which of the stores had the most orders?", 1, "DESC", "stores"),
        ("which one of our stores is the best", 1, "DESC", "stores"),
        ("Which customer segments drove the most historical revenue?", None, "DESC", "segments"),
        ("which product type sold the most", 1, "DESC", "type"),
        ("which 3 stores have the highest revenue", 3, "DESC", "stores"),
        ("highest revenue month in 2017", 1, "DESC", "month"),
        # A relative clause names the order; a plural fixes no count.
        ("the store with the most orders in 2017", 1, "DESC", "store"),
        ("the store with the fewest orders", 1, "ASC", "store"),
        ("Show the stores which have the highest revenue", None, "DESC", "stores"),
        ("3 stores with the highest revenue", 3, "DESC", "stores"),
        ("best 3 stores by revenue", 3, "DESC", "stores"),
        ("top-3 stores by revenue", 3, "DESC", "stores"),
    ],
)
def test_ranking_requests_parse(text: str, limit: int | None, direction: str, noun: str) -> None:
    request = _ranking_request(text)
    assert request is not None
    assert (request["limit"], request["direction"], request["noun"]) == (limit, direction, noun)


@pytest.mark.parametrize("text", ["rank stores by revenue", "stores ranked by revenue"])
def test_rank_by_asks_for_an_order_without_a_count(text: str) -> None:
    request = _ranking_request(text)
    assert request is not None
    assert (request["limit"], request["noun"], request["requires_order"]) == (
        None,
        "stores",
        True,
    )


def test_best_ranks_a_catalog_noun_only() -> None:
    assert _ranking_request("which one is the best store by revenue") is None
    request = _ranking_request("which one is the best store by revenue", frozenset({"store"}))
    assert request is not None and (request["limit"], request["noun"]) == (1, "store")


@pytest.mark.parametrize(
    "text",
    [
        "orders by store",
        "revenue in 2017",
        "orders at 3 stores",
        # Thresholds are not rankings.
        "monthly revenue from customers with at least 10 orders by store",
        "orders from customers with at most 3 visits",
        # Recency is a window, not a ranking by a measure.
        "most recent month revenue",
        "revenue for the 5 most recent months",
        # A share of a population is a threshold.
        "revenue from top decile of customers by lifetime spend",
        "orders from the top 10 percent of customers",
    ],
)
def test_non_rankings_are_not_rankings(text: str) -> None:
    assert _ranking_request(text) is None


def test_a_dropped_ranking_is_a_gap(adapter: SemanticLayerMCPAdapter) -> None:
    text = "the 3 lowest-selling products by item revenue"
    ranked = _query(
        ITEM_REVENUE,
        group_by=[PRODUCT],
        order_by=[{"field": "item_revenue_usd", "direction": "ASC"}],
        limit=3,
    )
    assert _gap_kinds(adapter, text, ranked) == []
    for broken in (
        {**ranked, "limit": None},
        {**ranked, "order_by": [{"field": "item_revenue_usd", "direction": "DESC"}]},
        # Sorted by the product's name, not by what ranks it.
        {**ranked, "order_by": [{"field": PRODUCT, "direction": "ASC"}]},
        {**ranked, "group_by": [STORE]},
    ):
        assert _gap_kinds(adapter, text, broken) == ["ranking_unrealized"]


def test_rank_by_must_sort_by_the_measure(adapter: SemanticLayerMCPAdapter) -> None:
    by_name = _query(group_by=[STORE], order_by=[{"field": STORE, "direction": "ASC"}])
    by_revenue = {**by_name, "order_by": [{"field": "revenue_usd", "direction": "DESC"}]}
    assert _gap_kinds(adapter, "rank stores by revenue", by_name) == ["ranking_unrealized"]
    assert _gap_kinds(adapter, "rank stores by revenue", by_revenue) == []
    # "the top store" is one store.
    top_five = {**by_revenue, "limit": 5}
    assert _gap_kinds(adapter, "the top store by revenue", top_five) == ["ranking_unrealized"]
    assert _gap_kinds(adapter, "the top store by revenue", {**top_five, "limit": 1}) == []


def test_ranking_uses_the_named_measure_when_two_are_selected(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    ranked = _query(
        group_by=[STORE], limit=5, order_by=[{"field": "order_count", "direction": "DESC"}]
    )
    ranked["select"].append(ORDERS)
    assert _gap_kinds(adapter, "top 5 stores by revenue", ranked) == ["ranking_unrealized"]
    correct = {**ranked, "order_by": [{"field": "revenue_usd", "direction": "DESC"}]}
    assert _gap_kinds(adapter, "top 5 stores by revenue", correct) == []
    disguised = {
        **correct,
        "select": [
            {"as": "revenue_usd", "expression": ORDERS["expression"]},
            {"as": "actual_revenue", "expression": REVENUE["expression"]},
        ],
    }
    assert _gap_kinds(adapter, "top 5 stores by revenue", disguised) == ["ranking_unrealized"]
    uncertain = _gaps(adapter, "top 5 stores", ranked)
    assert [gap["kind"] for gap in uncertain] == ["ranking_unrealized"]
    assert "ranked_measure_uncertain" in uncertain[0]["message"]


def _count_free_ranked_draft(
    order_by: list[dict[str, str]], *, limit: int | None = 3
) -> dict[str, Any]:
    draft = _query(group_by=[STORE], order_by=order_by)
    draft["select"].append(ORDERS)
    if limit is not None:
        draft["limit"] = limit
    return draft


@pytest.mark.parametrize(("word", "direction"), [("top", "DESC"), ("bottom", "ASC")])
@pytest.mark.parametrize("problem", ["missing", "reversed", "wrong_measure"])
def test_count_free_ranking_requires_the_requested_order_and_measure(
    adapter: SemanticLayerMCPAdapter, word: str, direction: str, problem: str
) -> None:
    order_by = {
        "missing": [],
        "reversed": [
            {"field": "revenue_usd", "direction": "ASC" if direction == "DESC" else "DESC"}
        ],
        "wrong_measure": [{"field": "order_count", "direction": direction}],
    }[problem]
    gaps = _gaps(adapter, f"{word} stores by revenue", _count_free_ranked_draft(order_by))
    assert [gap["kind"] for gap in gaps] == ["ranking_unrealized"]
    assert ("ranked_measure" if problem == "wrong_measure" else "order") in gaps[0]["message"]


@pytest.mark.parametrize(("word", "direction"), [("top", "DESC"), ("bottom", "ASC")])
@pytest.mark.parametrize("limit", [None, 3])
def test_count_free_ranking_keeps_correct_order_with_or_without_a_draft_limit(
    adapter: SemanticLayerMCPAdapter, word: str, direction: str, limit: int | None
) -> None:
    draft = _count_free_ranked_draft(
        [{"field": "revenue_usd", "direction": direction}], limit=limit
    )
    assert _gap_kinds(adapter, f"{word} stores by revenue", draft) == []


# --- time windows -------------------------------------------------------------


def test_a_dropped_or_different_window_is_a_gap(adapter: SemanticLayerMCPAdapter) -> None:
    unbounded = _query()
    assert _gap_kinds(adapter, "total revenue in 2017", unbounded) == ["time_window_unrealized"]
    bounded = _query(time={"temporal_role": ORDER_TIME, "grain": "year", **YEAR_2017})
    assert _gap_kinds(adapter, "total revenue in 2017", bounded) == []
    # A window, but not the one asked for.
    assert _gap_kinds(adapter, "revenue in Q2 2017", bounded) == ["time_window_unrealized"]


def test_a_lookback_metrics_dropped_start_is_left_to_plan(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    # plan reports TIME_WINDOW_START_DROPPED for a draft that keeps only the end.
    end_only = _query(time={"temporal_role": ORDER_TIME, "grain": "month", "end": "2018-01-01"})
    assert _gap_kinds(adapter, "revenue by month in 2017", end_only) == []


def test_a_prior_period_comparison_is_not_a_window(adapter: SemanticLayerMCPAdapter) -> None:
    # The dev split's correct answer to J32.
    alongside = {
        "version": 2,
        "select": [
            REVENUE,
            {
                "as": "revenue_prior_month",
                "expression": {
                    "kind": "prior_period",
                    "measure": "measure.jaffle.revenue_usd",
                    "offset": -1,
                    "grain": "month",
                },
            },
        ],
        "time": {"temporal_role": ORDER_TIME, "grain": "month"},
    }
    text = "monthly revenue alongside the previous month's revenue"
    assert _gap_kinds(adapter, text, alongside) == []
    # Last month's revenue alone doesn't answer it.
    last_month = _query(
        time={
            "temporal_role": ORDER_TIME,
            "grain": "month",
            "range": {"last": {"unit": "month", "value": 1}},
        }
    )
    assert _gap_kinds(adapter, text, last_month) == ["prior_period_comparison_unrealized"]


def test_a_callers_window_settles_the_question(adapter: SemanticLayerMCPAdapter) -> None:
    caller = {"time": {"start": "2017-03-01", "end": "2017-06-01"}}
    draft = _query(time={"temporal_role": ORDER_TIME, "grain": "month", **caller["time"]})
    text = "orders in March 2017"
    assert _gap_kinds(adapter, text, draft) == ["time_window_unrealized"]
    assert _gap_kinds(adapter, text, draft, partial_query=caller) == []


# --- filter values ------------------------------------------------------------


def test_every_named_value_must_reach_a_filter(adapter: SemanticLayerMCPAdapter) -> None:
    text = "revenue by store for Philadelphia and Brooklyn"
    both = _query(
        group_by=[STORE],
        where=[{"field": STORE, "op": "in", "value": ["Philadelphia", "Brooklyn"]}],
    )
    assert _gap_kinds(adapter, text, both) == []
    # Grouping by store doesn't bring back a store the filter drops.
    one = {**both, "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}]}
    assert _gap_kinds(adapter, text, one) == ["filter_values_unrealized"]


@pytest.mark.parametrize(
    ("where", "text", "honored"),
    [
        (
            [{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}],
            "revenue for Brooklyn and Philadelphia",
            True,
        ),
        (
            [
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]},
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "New Orleans"]},
            ],
            "revenue for Brooklyn and Philadelphia",
            False,
        ),
        (
            [
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]},
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
            ],
            "revenue for Brooklyn and Philadelphia",
            False,
        ),
        (
            [
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]},
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
            ],
            "revenue excluding Brooklyn, including Philadelphia",
            True,
        ),
    ],
)
def test_conjunctive_value_constraints_have_effective_intersection(
    adapter: SemanticLayerMCPAdapter,
    where: list[dict[str, Any]],
    text: str,
    honored: bool,
) -> None:
    kinds = _gap_kinds(adapter, text, _query(where=where))
    assert (kinds == []) is honored
    if not honored:
        assert "filter_values_unrealized" in kinds


def test_nested_value_scopes_do_not_supply_query_level_membership(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    query = _query(
        {
            "as": "brooklyn_revenue",
            "expression": {
                "kind": "scoped_aggregate",
                "measure": "measure.jaffle.revenue_usd",
                "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}],
            },
        },
        where=[{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}],
    )
    assert "filter_values_unrealized" in _gap_kinds(
        adapter, "revenue for Brooklyn and Philadelphia", query
    )
    separate = _query(
        {
            "as": "brooklyn_revenue",
            "expression": {
                "kind": "scoped_aggregate",
                "measure": "measure.jaffle.revenue_usd",
                "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}],
            },
        }
    )
    separate["select"].append(
        {
            "as": "philadelphia_revenue",
            "expression": {
                "kind": "scoped_aggregate",
                "measure": "measure.jaffle.revenue_usd",
                "where": [{"field": STORE, "op": "=", "value": "Philadelphia"}],
            },
        }
    )
    assert "filter_values_unrealized" in _gap_kinds(
        adapter, "revenue for Brooklyn and Philadelphia", separate
    )


@pytest.mark.parametrize(
    ("text", "field", "canonical", "literal", "op", "honored"),
    [
        ("item revenue for food products", PRODUCT_TYPE, "jaffle", "jaffle", "=", True),
        ("item revenue for food products", PRODUCT_TYPE, "jaffle", "Food", "=", False),
        ("item revenue excluding food products", PRODUCT_TYPE, "jaffle", "jaffle", "!=", True),
        ("item revenue excluding food products", PRODUCT_TYPE, "jaffle", "Food", "!=", False),
        ("revenue for Brooklyn", STORE, "Brooklyn", "Brooklyn", "=", True),
        ("revenue for Brooklyn", STORE, "Brooklyn", "brooklyn", "=", False),
        ("revenue excluding Brooklyn", STORE, "Brooklyn", "Brooklyn", "!=", True),
        ("revenue excluding Brooklyn", STORE, "Brooklyn", "brooklyn", "!=", False),
        ("revenue for New Orleans", STORE, "New Orleans", "New Orleans", "=", True),
        ("revenue for New Orleans", STORE, "New Orleans", "New-Orleans", "=", False),
        ("revenue excluding New Orleans", STORE, "New Orleans", "New Orleans", "!=", True),
        ("revenue excluding New Orleans", STORE, "New Orleans", "New-Orleans", "!=", False),
    ],
)
def test_named_question_values_require_executable_canonical_literals(
    adapter: SemanticLayerMCPAdapter,
    text: str,
    field: str,
    canonical: str,
    literal: str,
    op: str,
    honored: bool,
) -> None:
    select = ITEM_REVENUE if field == PRODUCT_TYPE else REVENUE
    gaps = _gaps(
        adapter, text, _query(select, where=[{"field": field, "op": op, "value": literal}])
    )
    assert (gaps == []) is honored
    if not honored:
        assert canonical in [
            value["value"]
            for gap in gaps
            if gap["kind"] == "filter_values_unrealized"
            for value in gap["expected"]["values"]
        ]


@pytest.mark.parametrize(
    ("text", "field", "canonical", "where", "sql_where", "source_values"),
    [
        (
            "revenue for Philadelphia",
            STORE,
            "Philadelphia",
            [
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]},
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "New Orleans"]},
            ],
            "v IN ('Brooklyn', 'Philadelphia') AND v IN ('Brooklyn', 'New Orleans')",
            ("Brooklyn", "Philadelphia", "New Orleans"),
        ),
        (
            "revenue for Philadelphia",
            STORE,
            "Philadelphia",
            [
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]},
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
            ],
            "v IN ('Brooklyn', 'Philadelphia') AND v NOT IN ('Brooklyn')",
            ("Brooklyn", "Philadelphia"),
        ),
        (
            "revenue for Brooklyn",
            STORE,
            "Brooklyn",
            [
                {"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]},
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
            ],
            "v IN ('Brooklyn', 'Philadelphia') AND v NOT IN ('Brooklyn')",
            ("Brooklyn", "Philadelphia"),
        ),
        (
            "revenue excluding Brooklyn",
            STORE,
            "Brooklyn",
            [{"field": STORE, "op": "!=", "value": "brooklyn"}],
            "v != 'brooklyn'",
            ("Brooklyn", "Philadelphia"),
        ),
        (
            "revenue excluding Brooklyn",
            STORE,
            "Brooklyn",
            [{"field": STORE, "op": "!=", "value": "Brooklyn"}],
            "v != 'Brooklyn'",
            ("Brooklyn", "Philadelphia"),
        ),
        (
            "item revenue for food products",
            PRODUCT_TYPE,
            "jaffle",
            [{"field": PRODUCT_TYPE, "op": "=", "value": "Food"}],
            "v = 'Food'",
            ("jaffle", "drink"),
        ),
        (
            "item revenue for food products",
            PRODUCT_TYPE,
            "jaffle",
            [{"field": PRODUCT_TYPE, "op": "=", "value": "jaffle"}],
            "v = 'jaffle'",
            ("jaffle", "drink"),
        ),
        (
            "revenue for New Orleans",
            STORE,
            "New Orleans",
            [{"field": STORE, "op": "=", "value": "New-Orleans"}],
            "v = 'New-Orleans'",
            ("New Orleans", "Brooklyn"),
        ),
    ],
)
def test_named_value_guard_agrees_with_executable_predicate_results(
    adapter: SemanticLayerMCPAdapter,
    text: str,
    field: str,
    canonical: str,
    where: list[dict[str, Any]],
    sql_where: str,
    source_values: tuple[str, ...],
) -> None:
    # This small, fixed DuckDB domain checks the observable behavior of the
    # supported operators. The guard must never credit a requested value that
    # the executable predicate removes (or a forbidden value that survives).
    relation = ", ".join(f"('{value}')" for value in source_values)
    connection = duckdb.connect(":memory:")
    try:
        actual = {
            row[0]
            for row in connection.execute(
                f"SELECT v FROM (VALUES {relation}) AS source(v) WHERE {sql_where}"
            ).fetchall()
        }
    finally:
        connection.close()
    assert canonical in source_values
    should_honor = canonical not in actual if "excluding" in text else canonical in actual
    select = ITEM_REVENUE if field == PRODUCT_TYPE else REVENUE
    gaps = _gap_kinds(adapter, text, _query(select, where=where))
    assert (gaps == []) is should_honor


def test_question_alias_maps_to_canonical_but_draft_alias_is_not_literal() -> None:
    runtime = _stand_in_runtime(["New York"])
    value = runtime._config.value_domains[0].values[0]
    value.aliases = ["Big Apple"]
    query = _query({"as": "revenue", "expression": {"measure": "measure.shop.revenue"}})
    for text, op in (("revenue in Big Apple", "="), ("revenue excluding Big Apple", "!=")):
        canonical = {
            **query,
            "where": [{"field": "dimension.region", "op": op, "value": "New York"}],
        }
        alias = {**query, "where": [{"field": "dimension.region", "op": op, "value": "Big Apple"}]}
        assert _filter_value_gaps(runtime, text, canonical) == []
        assert [gap.kind for gap in _filter_value_gaps(runtime, text, alias)] == [
            "filter_values_unrealized"
        ]


@pytest.mark.parametrize("op", ["!=", "NOT IN"])
def test_positive_requested_value_cannot_be_excluded(
    adapter: SemanticLayerMCPAdapter, op: str
) -> None:
    value = ["Brooklyn"] if op == "NOT IN" else "Brooklyn"
    excluded = _query(where=[{"field": STORE, "op": op, "value": value}])
    assert _gap_kinds(adapter, "revenue for Brooklyn", excluded) == ["filter_values_unrealized"]
    assert (
        _gap_kinds(
            adapter,
            "revenue for Brooklyn",
            _query(where=[{"field": STORE, "op": "=", "value": "Brooklyn"}]),
        )
        == []
    )
    both = {**excluded, "group_by": [STORE]}
    assert _gap_kinds(adapter, "revenue by store for Philadelphia and Brooklyn", both) == [
        "filter_values_unrealized"
    ]
    assert _gap_kinds(adapter, "revenue excluding Brooklyn", excluded) == []


def test_exclusion_must_name_the_requested_value(adapter: SemanticLayerMCPAdapter) -> None:
    wrong = _query(where=[{"field": STORE, "op": "!=", "value": "Philadelphia"}])
    assert _gap_kinds(adapter, "revenue excluding Brooklyn", wrong) == ["filter_values_unrealized"]


@pytest.mark.parametrize(
    ("text", "op", "value", "honored"),
    [
        ("revenue for Brooklyn", "=", "Brooklyn", True),
        ("revenue for Brooklyn", "IN", "Brooklyn", True),
        ("revenue for Brooklyn", "IN", ["Brooklyn", "Philadelphia"], True),
        ("revenue for Brooklyn", "IN", [], False),
        ("revenue for Brooklyn", "IN", [["Brooklyn"]], False),
        ("revenue for Brooklyn", "=", ["Brooklyn", "Philadelphia"], False),
        ("revenue for Brooklyn", "=", [["Brooklyn"]], False),
        ("revenue for Brooklyn", "!=", "Brooklyn", False),
        ("revenue for Brooklyn", "NOT IN", ["Brooklyn"], False),
        ("revenue for Brooklyn", "LIKE", "Brook%", False),
        ("revenue for Brooklyn", ">", "Brooklyn", False),
        ("revenue excluding Brooklyn", "!=", "Brooklyn", True),
        ("revenue excluding Brooklyn", "NOT IN", "Brooklyn", True),
        # Dropping Philadelphia too changes the total and removes its row.
        ("revenue excluding Brooklyn", "NOT IN", ["Brooklyn", "Philadelphia"], False),
        ("revenue excluding Brooklyn", "NOT IN", [], False),
        ("revenue excluding Brooklyn", "NOT IN", [["Brooklyn"]], False),
        ("revenue excluding Brooklyn", "!=", ["Brooklyn", "Philadelphia"], False),
        ("revenue excluding Brooklyn", "!=", [["Brooklyn"]], False),
        ("revenue excluding Brooklyn", "=", "Brooklyn", False),
        ("revenue excluding Brooklyn", "IN", ["Brooklyn"], False),
        ("revenue excluding Brooklyn", "NOT LIKE", "Brook%", False),
    ],
)
@pytest.mark.parametrize("group_by", [[STORE], []])
def test_named_value_coverage_respects_operator_and_value_shape(
    adapter: SemanticLayerMCPAdapter,
    text: str,
    op: str,
    value: Any,
    honored: bool,
    group_by: list[str],
) -> None:
    if not group_by and op == "IN" and value == ["Brooklyn", "Philadelphia"]:
        # Without a per-store row, the extra store changes the single total.
        honored = False
    draft = _query(group_by=group_by, where=[{"field": STORE, "op": op, "value": value}])
    kinds = _gap_kinds(adapter, text, draft)
    if honored:
        assert kinds == []
    else:
        assert kinds
        assert "filter_values_unrealized" in kinds or "negation_reversed" in kinds


@pytest.mark.parametrize(
    ("where", "honored"),
    [
        (
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                {"field": STORE, "op": "=", "value": "Philadelphia"},
            ],
            True,
        ),
        (
            [
                {"field": STORE, "op": "!=", "value": ["Brooklyn"]},
                {"field": STORE, "op": "=", "value": "Philadelphia"},
            ],
            False,
        ),
        ([{"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "Philadelphia"]}], False),
        # Dropping New Orleans changes nothing once only Philadelphia is kept.
        (
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "New Orleans"]},
                {"field": STORE, "op": "=", "value": "Philadelphia"},
            ],
            True,
        ),
        (
            [
                {"field": STORE, "op": "=", "value": ["Philadelphia"]},
                {"field": STORE, "op": "!=", "value": "Brooklyn"},
            ],
            False,
        ),
    ],
)
def test_mixed_value_coverage_respects_each_values_polarity_and_shape(
    adapter: SemanticLayerMCPAdapter, where: list[dict[str, Any]], honored: bool
) -> None:
    draft = _query(where=where)
    kinds = _gap_kinds(adapter, "revenue excluding Brooklyn, including Philadelphia", draft)
    assert ("filter_values_unrealized" not in kinds) is honored


@pytest.mark.parametrize(
    "text",
    [
        "revenue excluding Brooklyn, including Philadelphia",
        "revenue excluding Brooklyn including Philadelphia",
        "revenue excluding Brooklyn, and including Philadelphia",
        "revenue excluding Brooklyn but include Philadelphia",
        "revenue not including Brooklyn, including Philadelphia",
    ],
)
def test_explicit_inclusion_ends_exclusion_scope(
    adapter: SemanticLayerMCPAdapter, text: str
) -> None:
    wrong = _query(where=[{"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "Philadelphia"]}])
    assert _gap_kinds(adapter, text, wrong) == ["filter_values_unrealized"]
    for exclusion in ({"op": "!=", "value": "Brooklyn"}, {"op": "NOT IN", "value": ["Brooklyn"]}):
        correct = _query(
            where=[
                {"field": STORE, **exclusion},
                {"field": STORE, "op": "=", "value": "Philadelphia"},
            ]
        )
        assert _gap_kinds(adapter, text, correct) == []


def test_comma_separated_exclusions_remain_negative(adapter: SemanticLayerMCPAdapter) -> None:
    draft = _query(where=[{"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "Philadelphia"]}])
    assert _gap_kinds(adapter, "revenue excluding Brooklyn, Philadelphia", draft) == []


@pytest.mark.parametrize(
    ("text", "where", "sql_where", "expected_gaps", "second_survives"),
    [
        (
            "revenue excluding Brooklyn; excluding New Orleans",
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                {"field": STORE, "op": "IN", "value": ["Philadelphia", "New Orleans"]},
            ],
            "v NOT IN ('Brooklyn') AND v IN ('Philadelphia', 'New Orleans')",
            ["negation_reversed"],
            True,
        ),
        (
            "revenue excluding Brooklyn, excluding New Orleans",
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                {"field": STORE, "op": "IN", "value": ["Philadelphia", "New Orleans"]},
            ],
            "v NOT IN ('Brooklyn') AND v IN ('Philadelphia', 'New Orleans')",
            ["negation_reversed"],
            True,
        ),
        (
            "revenue excluding Brooklyn and excluding New Orleans",
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                {"field": STORE, "op": "IN", "value": ["Philadelphia", "New Orleans"]},
            ],
            "v NOT IN ('Brooklyn') AND v IN ('Philadelphia', 'New Orleans')",
            ["negation_reversed"],
            True,
        ),
        (
            "revenue excluding Brooklyn, excluding New Orleans, including Philadelphia",
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                {"field": STORE, "op": "IN", "value": ["Philadelphia", "New Orleans"]},
            ],
            "v NOT IN ('Brooklyn') AND v IN ('Philadelphia', 'New Orleans')",
            ["negation_reversed"],
            True,
        ),
        (
            "revenue for all stores but Brooklyn; excluding New Orleans",
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                {"field": STORE, "op": "IN", "value": ["Philadelphia", "New Orleans"]},
            ],
            "v NOT IN ('Brooklyn') AND v IN ('Philadelphia', 'New Orleans')",
            ["negation_reversed"],
            True,
        ),
        (
            "revenue excluding Brooklyn; except New Orleans",
            [{"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]}],
            "v NOT IN ('Brooklyn')",
            ["filter_values_unrealized"],
            True,
        ),
        (
            "revenue excluding Brooklyn; without New Orleans, including Philadelphia",
            [
                {"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "New Orleans"]},
                {"field": STORE, "op": "=", "value": "Philadelphia"},
            ],
            "v NOT IN ('Brooklyn', 'New Orleans') AND v = 'Philadelphia'",
            [],
            False,
        ),
        (
            "revenue for all stores but Brooklyn; excluding New Orleans",
            [{"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "New Orleans"]}],
            "v NOT IN ('Brooklyn', 'New Orleans')",
            [],
            False,
        ),
        (
            "revenue excluding Brooklyn and excluding New Orleans",
            [{"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "New Orleans"]}],
            "v NOT IN ('Brooklyn', 'New Orleans')",
            [],
            False,
        ),
        (
            "revenue excluding Brooklyn and New Orleans",
            [{"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "New Orleans"]}],
            "v NOT IN ('Brooklyn', 'New Orleans')",
            [],
            False,
        ),
    ],
)
def test_each_exclusion_clause_is_checked_against_executable_predicates(
    adapter: SemanticLayerMCPAdapter,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    where: list[dict[str, Any]],
    sql_where: str,
    expected_gaps: list[str],
    second_survives: bool,
) -> None:
    draft = _query(where=where)
    gaps = _gaps(adapter, text, draft)
    assert [gap["kind"] for gap in gaps] == expected_gaps
    if expected_gaps == ["negation_reversed"]:
        assert gaps[0]["clause"] == "excluding New Orleans"
    _draft_plan(monkeypatch, draft)
    payload = adapter.call_tool("plan", {"intent": text, "detail": "query"})
    assert payload["best"]["validation_ok"] is True
    assert payload["status"] == ("low_confidence" if expected_gaps else "ok")
    if expected_gaps:
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert expected_gaps == [gap["kind"] for gap in payload["why"]["details"]["gaps"]]
    else:
        assert payload.get("why") is None
    connection = duckdb.connect(":memory:")
    try:
        retained = {
            row[0]
            for row in connection.execute(
                "SELECT v FROM (VALUES ('Brooklyn'), ('Philadelphia'), ('New Orleans')) "
                f"AS stores(v) WHERE {sql_where}"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "Brooklyn" not in retained
    assert ("New Orleans" in retained) is second_survives


def test_disjunctive_or_nested_exclusion_evidence_is_not_credited(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    # The top-level evidence model intersects only known predicates. It must
    # not combine an OR branch or a selected expression's private scope with it.
    disjunction = _query(
        where=[
            {
                "op": "OR",
                "args": [
                    {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                    {"field": STORE, "op": "IN", "value": ["New Orleans"]},
                ],
            }
        ]
    )
    assert "filter_values_unrealized" in _gap_kinds(
        adapter, "revenue excluding Brooklyn; excluding New Orleans", disjunction
    )
    nested = _query(
        {
            "as": "revenue_usd",
            "expression": {
                "kind": "scoped_aggregate",
                "measure": "measure.jaffle.revenue_usd",
                "where": [{"field": STORE, "op": "NOT IN", "value": ["New Orleans"]}],
            },
        },
        where=[{"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]}],
    )
    assert "filter_values_unrealized" in _gap_kinds(
        adapter, "revenue excluding Brooklyn; excluding New Orleans", nested
    )


@pytest.mark.parametrize("text", ["revenue not including Brooklyn", "revenue not include Brooklyn"])
def test_negated_include_remains_an_exclusion(adapter: SemanticLayerMCPAdapter, text: str) -> None:
    excluded = _query(where=[{"field": STORE, "op": "!=", "value": "Brooklyn"}])
    included = _query(where=[{"field": STORE, "op": "=", "value": "Brooklyn"}])
    assert _gap_kinds(adapter, text, excluded) == []
    assert _gap_kinds(adapter, text, included) == ["negation_reversed"]


@pytest.mark.parametrize(
    ("text", "query"),
    [
        # "New Orleans" masks the "new" inside it.
        ("revenue in New Orleans", _query(where=[{"field": STORE, "value": "New Orleans"}])),
        (
            "revenue for New Orleans by month",
            _query(
                where=[{"field": STORE, "op": "=", "value": "New Orleans"}],
                time={"temporal_role": ORDER_TIME, "grain": "month"},
            ),
        ),
        (
            "revenue for all stores except New Orleans",
            _query(group_by=[STORE], where=[{"field": STORE, "op": "!=", "value": "New Orleans"}]),
        ),
        # Grouping by a value's dimension shows it as a row.
        ("revenue by product type, food vs drink", _query(ITEM_REVENUE, group_by=[PRODUCT_TYPE])),
        ("orders by customer type, new vs repeat", _query(ORDERS, group_by=[CUSTOMER_TYPE])),
        # "new" names a customer type only next to a word of that dimension.
        (
            "new store revenue by month",
            _query(time={"temporal_role": ORDER_TIME, "grain": "month"}),
        ),
    ],
)
def test_honored_values_are_not_gaps(
    adapter: SemanticLayerMCPAdapter, text: str, query: dict[str, Any]
) -> None:
    assert _gap_kinds(adapter, text, query) == []


def test_values_the_draft_ignores_are_gaps(adapter: SemanticLayerMCPAdapter) -> None:
    # "Food" is the label of the value 'jaffle'; the package name in every id
    # doesn't stand in for it.
    food = _gaps(adapter, "orders that included food", _query(ORDERS))
    assert [gap["kind"] for gap in food] == ["filter_values_unrealized"]
    assert food[0]["clause"] == "food"
    assert food[0]["expected"]["values"][0]["value"] == "jaffle"
    new_customers = _gap_kinds(adapter, "revenue for new customers", _query())
    assert new_customers == ["filter_values_unrealized"]
    honored = _query(where=[{"field": CUSTOMER_TYPE, "value": "new"}])
    assert _gap_kinds(adapter, "revenue for new customers", honored) == []


def test_contradictory_filters_are_a_gap(adapter: SemanticLayerMCPAdapter) -> None:
    both = _query(
        where=[
            {"field": STORE, "op": "=", "value": "Philadelphia"},
            {"field": STORE, "op": "=", "value": "Brooklyn"},
        ],
        time={"temporal_role": ORDER_TIME, "grain": "month"},
    )
    gaps = _gaps(adapter, "Philadelphia vs Brooklyn revenue by month", both)
    assert [gap["kind"] for gap in gaps] == ["contradictory_filters"]
    assert gaps[0]["actual"]["conflicts"] == [
        {"field": STORE, "values": ["Brooklyn", "Philadelphia"]}
    ]
    merged = {
        **both,
        "where": [{"field": STORE, "op": "in", "value": ["Philadelphia", "Brooklyn"]}],
    }
    assert _gap_kinds(adapter, "Philadelphia vs Brooklyn revenue by month", merged) == []
    excluded = {
        **both,
        "where": [
            {"field": STORE, "op": "IN", "value": ["Brooklyn"]},
            {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
        ],
    }
    assert "contradictory_filters" in _gap_kinds(adapter, "revenue for Brooklyn", excluded)


def _stand_in_runtime(values: list[Any]) -> Any:
    def row(object_id: str, name: str) -> Any:
        return SimpleNamespace(
            id=object_id, name=name, label=name, description="", aliases=[], topics=[]
        )

    return SimpleNamespace(
        _config=SimpleNamespace(
            measures=[row("measure.shop.revenue", "revenue")],
            metric_recipes=[],
            dimensions=[row("dimension.store_name", "store"), row("dimension.region", "region")],
            entities=[],
            segments=[],
            temporal_roles=[],
            value_domains=[
                SimpleNamespace(
                    dimensions=["dimension.region"],
                    values=[
                        SimpleNamespace(value=value, label=str(value), aliases=[])
                        for value in values
                    ],
                )
            ],
        )
    )


@pytest.mark.parametrize(
    "text",
    [
        "show us revenue by store",
        "revenue by store for all stores",
        "can you open a report of revenue by store",
        "revenue by store, other than that",
        "top 3 stores by revenue",
        "revenue by store over the last 2 weeks",
    ],
)
def test_everyday_words_and_numbers_are_not_values(text: str) -> None:
    runtime = _stand_in_runtime(["US", "all", "Other", "open", 1, 2, 3])
    query = _query({"as": "revenue", "expression": {"measure": "measure.shop.revenue"}})
    assert _filter_value_gaps(runtime, text, {**query, "group_by": ["dimension.store_name"]}) == []


# --- unmatched terms ----------------------------------------------------------


def test_unmatched_terms(adapter: SemanticLayerMCPAdapter) -> None:
    revenue = _query()
    runtime = adapter.runtime
    assert unmatched_intent_terms(runtime, "cumulative revenue by month", revenue) == ["cumulative"]
    assert unmatched_intent_terms(
        runtime, "what's the weather in Philadelphia tomorrow", revenue
    ) == [
        "weather",
        "philadelphia",
        "tomorrow",
    ]
    # Framing words, plurals and one-letter typos are accounted for.
    assert (
        unmatched_intent_terms(runtime, "show me the total revnue by month for 2017", revenue) == []
    )
    assert unmatched_intent_terms(runtime, "revnue by stor", _query(group_by=[STORE])) == []


@pytest.mark.parametrize(
    "text",
    [
        # Time phrases the planner reads, and ordinals inside them.
        "revenue over the past 3 months",
        "revenue for the previous quarter",
        "revenue in the trailing 12 months",
        "revenue yesterday",
        "revenue on April 7th, 2017",
        "revenue from Jan 2017 thru Mar 2017",
        "revenue by month in the 1st half of 2017",
        # Request words.
        "I'd like to see revenue by month",
        "Let's break down revenue by month",
        "calculate the revenue trend by month",
    ],
)
def test_framing_is_not_unmatched(adapter: SemanticLayerMCPAdapter, text: str) -> None:
    draft = _query(time={"temporal_role": ORDER_TIME, "grain": "month"})
    assert unmatched_intent_terms(adapter.runtime, text, draft) == []


def test_near_misses_are_reported_in_the_questions_words(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    runtime = adapter.runtime
    revenue = _query()
    # "online" isn't a typo of "line", nor "next" of "net".
    assert unmatched_intent_terms(runtime, "revenue from online orders", revenue) == ["online"]
    assert "next" in unmatched_intent_terms(runtime, "revenue next year", revenue)
    # The question's own spelling, not the planner's normalized token.
    by_store = _query(group_by=[STORE])
    assert unmatched_intent_terms(runtime, "revenue percent by store", by_store) == ["percent"]
    many = " ".join(f"word{letter}" for letter in "abcdefghijklmnop")
    assert len(unmatched_intent_terms(runtime, f"revenue {many}", revenue)) == 8


def test_an_entity_in_a_predicate_accounts_for_its_word(adapter: SemanticLayerMCPAdapter) -> None:
    qualified = _query(
        group_by=[STORE],
        metric_filters=[
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer",
                    "scope_mode": "entity_only",
                    "input": {"measure": "measure.jaffle.order_count"},
                    "op": ">=",
                    "value": 10,
                },
                "op": "=",
                "value": True,
            }
        ],
    )
    text = "revenue from customers with at least 10 orders by store"
    assert unmatched_intent_terms(adapter.runtime, text, qualified) == []


# --- the MCP plan response ----------------------------------------------------


def _draft_plan(monkeypatch: pytest.MonkeyPatch, query: dict[str, Any] | None) -> None:
    """Make plan draft exactly ``query`` (or nothing), whatever the planner would do."""

    from semantic_rails.planner import plan as plan_module
    from semantic_rails.planner._base import RuntimeCompositionDraft
    from semantic_rails.planner.orchestrator import CompositionResult

    def compose(runtime: Any, intent: str) -> CompositionResult:
        draft = (
            RuntimeCompositionDraft(query=query, resolved=[], rationale=[], interpreted_intent={})
            if query is not None
            else None
        )
        return CompositionResult(
            intent_ir=parse_intent(runtime, intent), draft=draft, pattern="test"
        )

    monkeypatch.setattr(plan_module, "compose", compose)
    monkeypatch.setattr(plan_module, "_distinct_fallback_drafts", lambda *args, **kwargs: [])


def test_mcp_plan_reports_what_it_could_not_honor(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = adapter.call_tool("plan", {"intent": "total revenue in 2017"})
    assert clean["status"] == "ok"
    assert clean["warnings"] == []
    weather = adapter.call_tool("plan", {"intent": "what's the weather in Philadelphia tomorrow"})
    codes = [warning["code"] for warning in weather["warnings"]]
    assert "PLAN_UNMATCHED_TERMS" in codes
    # A draft that drops the ranked products validates, but isn't ready.
    _draft_plan(
        monkeypatch,
        _query(
            ITEM_REVENUE,
            order_by=[{"field": "item_revenue_usd", "direction": "DESC"}],
            limit=5,
            time={"temporal_role": ORDER_TIME, "grain": "year", **YEAR_2017},
        ),
    )
    ranked = adapter.call_tool("plan", {"intent": "top 5 products by revenue in 2017"})
    assert ranked["best"]["validation_ok"] is True
    assert ranked["status"] == "low_confidence"
    assert ranked["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"


BROOKLYN_REVENUE = {
    "as": "brooklyn_revenue",
    "expression": {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.revenue_usd",
        "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}],
    },
}


@pytest.mark.parametrize(
    ("text", "draft", "status"),
    [
        (
            "revenue for Brooklyn",
            _query(where=[{"field": STORE, "op": "IN", "value": "Brooklyn"}]),
            "ok",
        ),
        (
            "revenue excluding Brooklyn",
            _query(where=[{"field": STORE, "op": "NOT IN", "value": "Brooklyn"}]),
            "ok",
        ),
        # One total may keep, or drop, only the values the question names.
        (
            "revenue for Brooklyn and Philadelphia",
            _query(where=[{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}]),
            "ok",
        ),
        (
            "revenue for Brooklyn",
            _query(where=[{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}]),
            "low_confidence",
        ),
        (
            "revenue excluding Brooklyn",
            _query(where=[{"field": STORE, "op": "NOT IN", "value": ["Brooklyn", "Philadelphia"]}]),
            "low_confidence",
        ),
        (
            "top 5 stores by revenue",
            {
                "version": 2,
                "select": [REVENUE, ORDERS],
                "group_by": [STORE],
                "order_by": [{"field": "revenue_usd", "direction": "DESC"}],
                "limit": 5,
            },
            "ok",
        ),
        (
            "revenue not include Brooklyn",
            _query(where=[{"field": STORE, "op": "!=", "value": "Brooklyn"}]),
            "ok",
        ),
        (
            "revenue excluding Brooklyn but include Philadelphia",
            _query(
                where=[
                    {"field": STORE, "op": "NOT IN", "value": ["Brooklyn"]},
                    {"field": STORE, "op": "=", "value": "Philadelphia"},
                ]
            ),
            "ok",
        ),
        # A nested scope validates, but doesn't supply query-level membership.
        (
            "revenue for Brooklyn and Philadelphia",
            _query(
                BROOKLYN_REVENUE,
                where=[{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}],
            ),
            "low_confidence",
        ),
    ],
)
def test_mcp_plan_validates_and_gates_guard_drafts(
    adapter: SemanticLayerMCPAdapter,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    draft: dict[str, Any],
    status: str,
) -> None:
    # The direct guard tests assume the draft validates and the question passes
    # scope gating; this checks both for the shapes and wordings they rely on.
    _draft_plan(monkeypatch, draft)
    payload = adapter.call_tool("plan", {"intent": text})
    assert payload["best"]["validation_ok"] is True
    assert payload["status"] == status


@pytest.mark.parametrize(
    ("text", "where", "op"),
    [
        (
            "revenue for Brooklyn and Philadelphia",
            [{"field": STORE, "op": "=", "value": ["Brooklyn", "Philadelphia"]}],
            "=",
        ),
        (
            "revenue excluding Brooklyn, Philadelphia",
            [{"field": STORE, "op": "!=", "value": ["Brooklyn", "Philadelphia"]}],
            "!=",
        ),
        (
            "revenue excluding Brooklyn, including Philadelphia",
            [
                {"field": STORE, "op": "!=", "value": ["Brooklyn"]},
                {"field": STORE, "op": "=", "value": "Philadelphia"},
            ],
            "!=",
        ),
    ],
)
def test_mcp_plan_rejects_list_values_for_scalar_filter_ops(
    adapter: SemanticLayerMCPAdapter,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    where: list[dict[str, Any]],
    op: str,
) -> None:
    # Current Query IR validation rejects this shape before the C1 guard.
    # Direct guard controls above separately assert its classification.
    _draft_plan(monkeypatch, _query(where=where))
    payload = adapter.call_tool("plan", {"intent": text})
    assert payload["best"]["validation_ok"] is False
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "VALIDATION_FAILED"
    assert any(
        error["code"] == "INVALID_QUERY"
        and f"op '{op}'" in error["message"]
        and "list" in error["message"]
        for error in payload["why"]["errors"]
    )


def test_mcp_plan_keeps_the_v1_best_default(adapter: SemanticLayerMCPAdapter) -> None:
    default = adapter.call_tool("plan", {"intent": "revenue by store"})
    assert default["best"]["query_ir"]
    assert "intent_ir" in default and "next" in default
    assert "trace" in default["best"]
    # An unknown detail level gets the same default.
    unknown = adapter.call_tool("plan", {"intent": "revenue by store", "detail": "brief"})
    envelope = {"request_id", "timing_ms"}
    assert {key: value for key, value in unknown.items() if key not in envelope} == {
        key: value for key, value in default.items() if key not in envelope
    }
    compact = adapter.call_tool("plan", {"intent": "revenue by store", "detail": "query"})
    for key in ("intent_ir", "next"):
        assert key not in compact
    assert "trace" not in compact["best"]


def test_an_unrealizable_plan_keeps_the_hints_its_why_names(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _draft_plan(monkeypatch, None)
    payload = adapter.call_tool("plan", {"intent": "revenue by store"})
    assert payload["status"] == "unrealizable"
    assert "compose_hints" in payload["why"]["message"]
    assert payload["compose_hints"]


def test_a_ranked_period_needs_its_grain(adapter: SemanticLayerMCPAdapter) -> None:
    daily = _query(
        order_by=[{"field": "revenue_usd", "direction": "DESC"}],
        limit=1,
        time={"temporal_role": ORDER_TIME, "grain": "day", **YEAR_2017},
    )
    text = "which month had the highest revenue in 2017"
    assert _gap_kinds(adapter, text, daily) == ["ranking_unrealized"]
    monthly = {**daily, "time": {**daily["time"], "grain": "month"}}
    assert _gap_kinds(adapter, text, monthly) == []


def test_all_but_is_an_exclusion(adapter: SemanticLayerMCPAdapter) -> None:
    inverted = _query(
        ORDERS,
        where=[{"field": STORE, "op": "=", "value": "Brooklyn"}],
        time={"temporal_role": ORDER_TIME, "grain": "month", **YEAR_2017},
    )
    text = "monthly orders in 2017 for all stores but Brooklyn"
    assert _gap_kinds(adapter, text, inverted) == ["negation_reversed"]


def test_pattern_filter_cannot_prove_an_exact_named_value(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    draft = _query(where=[{"field": STORE, "op": "LIKE", "value": "Brook%"}])
    assert _gap_kinds(adapter, "revenue in Brooklyn", draft) == ["filter_values_unrealized"]


def test_the_longest_value_wins() -> None:
    runtime = _stand_in_runtime(["New York", "York"])
    query = _query(
        {"as": "revenue", "expression": {"measure": "measure.shop.revenue"}},
        where=[{"field": "dimension.region", "op": "=", "value": "New York"}],
    )
    assert _filter_value_gaps(runtime, "revenue in New York", query) == []


def test_unmatched_terms_read_only_so_far(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from semantic_rails.planner import faithfulness

    monkeypatch.setattr(faithfulness, "_MAX_SCANNED_WORDS", 3)
    # "weather" is the fifth distinct word, past the three scanned.
    words = unmatched_intent_terms(adapter.runtime, "revenue by month for weather", _query())
    assert words == []


def test_a_typo_keeps_its_first_letter(adapter: SemanticLayerMCPAdapter) -> None:
    assert unmatched_intent_terms(adapter.runtime, "evenue by month", _query()) == ["evenue"]


def test_a_filtered_values_label_is_accounted_for(adapter: SemanticLayerMCPAdapter) -> None:
    food = _query(ITEM_REVENUE, where=[{"field": PRODUCT_TYPE, "value": "jaffle"}])
    assert unmatched_intent_terms(adapter.runtime, "item revenue for food products", food) == []


def test_an_unresolved_window_stays_visible_beside_other_gaps(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = _query(
        where=[
            {"field": STORE, "op": "=", "value": "Brooklyn"},
            {"field": STORE, "op": "=", "value": "Chicago"},
        ]
    )
    _draft_plan(monkeypatch, both)
    payload = adapter.call_tool(
        "plan", {"intent": "revenue for Brooklyn and Chicago since March 2017"}
    )
    kinds = [gap["kind"] for gap in payload["why"]["details"]["gaps"]]
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "contradictory_filters" in kinds and "time_window_unresolved" in kinds


# --- metrics the question names ---------------------------------------------

# A filtered and a rolling metric, as the authoring wizard writes them; the
# wizard suggests asking "What is <label> by <grain>?".
NAMED_METRICS = {
    "sales.large_order_revenue": {
        "as": "metric.sales.large_order_revenue",
        "label": "Large order revenue",
        "kind": "aggregate",
        "value_type": "currency",
        "currency": "USD",
        "temporal_role": ORDER_TIME,
        "expression": {
            "kind": "aggregate",
            "measure": "measure.jaffle.revenue_usd",
            "aggregation": "sum",
            "filter": {
                "all": [
                    {"field": "dimension.jaffle_order_is_large_order", "op": "IN", "value": [True]}
                ]
            },
        },
    },
    "sales.revenue_trailing_7_days": {
        "as": "metric.sales.revenue_trailing_7_days",
        "label": "Revenue, trailing 7 days",
        "kind": "rolling",
        "measure": "revenue_usd",
        "aggregation": "sum",
        "window": {"unit": "day", "value": 7},
        "value_type": "currency",
        "currency": "USD",
        "temporal_role": ORDER_TIME,
    },
}


@pytest.fixture()
def named_metrics(tmp_path: Path) -> Iterator[SemanticLayerMCPAdapter]:
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    extra = package / "metrics" / "extensions" / "named_metrics.yml"
    extra.write_text(yaml.safe_dump({"metrics": NAMED_METRICS}), encoding="utf-8")
    mcp = SemanticLayerMCPAdapter(Runtime.from_path(str(package)))
    try:
        yield mcp
    finally:
        mcp.close()


@pytest.mark.parametrize(
    ("text", "subject", "status", "gap"),
    [
        # The wizard's questions answer with the metric they name, by label or id,
        # and the label's own words are not also a window or a top 7.
        ("What is large order revenue by month?", "metric.sales.large_order_revenue", "ok", None),
        (
            "What is revenue, trailing 7 days by day?",
            "metric.sales.revenue_trailing_7_days",
            "ok",
            None,
        ),
        (
            "metric.sales.large_order_revenue by month",
            "metric.sales.large_order_revenue",
            "ok",
            None,
        ),
        ("cumulative revenue by month", "metric.sales.cumulative_revenue", "ok", None),
        # A measure with the metric's own name still means the metric, not plain revenue.
        ("rolling 28-day revenue by day", "metric.sales.rolling_28d_revenue", "ok", None),
        ("Revenue QTD by day", "metric.sales.revenue_qtd", "ok", None),
        # A draft without the named metric, or a second subject, isn't ready.
        (
            "large order revenue vs prior month by month",
            None,
            "low_confidence",
            "named_metric_unrealized",
        ),
        (
            "large order revenue and orders by month",
            None,
            "low_confidence",
            "multiple_subjects_unrealized",
        ),
        (
            "revenue and orders by month",
            "measure.jaffle.revenue_usd",
            "low_confidence",
            "multiple_subjects_unrealized",
        ),
        # "Count" and question words are filler on both sides of the match.
        (
            "What is order count and revenue by month?",
            None,
            "low_confidence",
            "multiple_subjects_unrealized",
        ),
        (
            "how many orders and revenue by month",
            None,
            "low_confidence",
            "multiple_subjects_unrealized",
        ),
        # A filter on a named dimension whose value the catalog doesn't declare.
        (
            "revenue where customer order number is 1",
            None,
            "low_confidence",
            "dimension_filter_unrealized",
        ),
    ],
)
def test_plan_answers_or_flags_the_metric_a_question_names(
    named_metrics: SemanticLayerMCPAdapter,
    text: str,
    subject: str | None,
    status: str,
    gap: str | None,
) -> None:
    payload = named_metrics.call_tool("plan", {"intent": text})
    query = payload["best"]["query_ir"]
    assert payload["status"] == status
    if subject is not None:
        assert [
            row["expression"].get("metric") or row["expression"].get("measure")
            for row in query["select"]
        ] == [subject]
    if status == "ok":
        assert "range" not in query["time"] and "limit" not in query
    else:
        assert gap in [row["kind"] for row in payload["why"]["details"]["gaps"]]


def test_a_where_clause_is_honored_by_a_filter_on_its_dimension(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    text = "revenue where customer order number is 1"
    number = "dimension.jaffle_order_customer_order_number"
    assert _gap_kinds(adapter, text, _query()) == ["dimension_filter_unrealized"]
    assert _gap_kinds(adapter, text, _query(where=[{"field": number, "op": "=", "value": 1}])) == []


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("large order revenue by month", "metric.sales.large_order_revenue"),
        # Revenue named again outside the metric's name leaves the measure first.
        ("large order revenue minus revenue by month", None),
        ("revenue minus large order revenue by month", None),
    ],
)
def test_a_metric_is_named_only_around_every_measure_named(
    named_metrics: SemanticLayerMCPAdapter, text: str, named: str | None
) -> None:
    found = _named_metric(named_metrics.runtime._config, text)
    assert (found[0].id if found else None) == named


@pytest.mark.parametrize(
    ("intent", "limit"),
    [
        # A number in a time phrase sizes the window; it doesn't make a top N.
        ("What is revenue in the last 3 months by store?", None),
        ("What was revenue from January 1 2017 to March 31 2017 by store?", None),
        ("What was revenue in 2017 by store?", None),
        ("which 3 stores have the highest revenue in the last 6 months", 3),
    ],
)
def test_a_number_in_a_time_phrase_is_not_a_ranking(
    adapter: SemanticLayerMCPAdapter, intent: str, limit: int | None
) -> None:
    plan = adapter.call_tool("plan", {"intent": intent, "detail": "query"})
    assert plan["status"] == "ok"
    assert plan["best"]["query_ir"].get("limit") == limit


Q1_2017 = {"start": "2017-01-01", "end": "2017-04-01"}


@pytest.mark.parametrize(
    ("intent", "group_by", "time"),
    [
        # "Order date" names the order clock; it used to group by Customer first order at.
        (
            "revenue by store and order date at month grain, from January 1 2017 to March 31 2017",
            [STORE],
            {"grain": "month", **Q1_2017},
        ),
        (
            "revenue by store and order month from January 1 2017 to March 31 2017",
            [STORE],
            {
                "grain": "month",
                **Q1_2017,
            },
        ),
        ("revenue by store by order month", [STORE], {"grain": "month"}),
        # By a date means by day; "by order date" used to be dropped after "by store".
        ("revenue by store by order date", [STORE], {"grain": "day"}),
        ("revenue by store and order date", [STORE], {"grain": "day"}),
        ("revenue by order date", [], {"grain": "day"}),
        # The grouping sets the grain; a unit a window names doesn't replace it.
        ("revenue by order date for the first quarter of 2017", [], {"grain": "day", **Q1_2017}),
        ("revenue by store by order date in 2017", [STORE], {"grain": "day", **YEAR_2017}),
        # A cadence the question names still sets the grain; the grouping's own unit comes first.
        ("monthly revenue by order date", [], {"grain": "month"}),
        ("weekly revenue by store by order date", [STORE], {"grain": "week"}),
        ("revenue by order date, at week grain", [], {"grain": "week"}),
        ("weekly revenue by store by order month", [STORE], {"grain": "month"}),
    ],
)
def test_a_named_order_date_is_the_order_clock(
    adapter: SemanticLayerMCPAdapter, intent: str, group_by: list[str], time: dict[str, str]
) -> None:
    plan = adapter.call_tool("plan", {"intent": intent, "detail": "query"})
    query = plan["best"]["query_ir"]
    assert plan["status"] == "ok"
    # "At <unit> grain" names no catalog object, so the plan reports "grain" as unmatched.
    grain_only = [{"code": "PLAN_UNMATCHED_TERMS", "terms": ["grain"]}] if "grain" in intent else []
    warnings = [{"code": w["code"], "terms": w["details"]["terms"]} for w in plan["warnings"]]
    assert warnings == grain_only
    assert query.get("group_by", []) == group_by
    assert query["time"] == {"temporal_role": ORDER_TIME, **time}


def test_the_order_date_answer_groups_by_store_and_month_only(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    intent = "revenue by store and order date at month grain, from January 1 2017 to March 31 2017"
    query = adapter.call_tool("plan", {"intent": intent, "detail": "query"})["best"]["query_ir"]
    rows = adapter.call_tool("execute", {"query": query})["rows"]
    assert len(rows) == 4  # Philadelphia for three months, Brooklyn from March
    assert {key for row in rows for key in row} == {
        STORE,
        f"{ORDER_TIME}__month",
        "revenue_usd",
    }


def test_another_clocks_date_is_not_the_order_clock(adapter: SemanticLayerMCPAdapter) -> None:
    plan = adapter.call_tool(
        "plan", {"intent": "revenue by store and customer first order date", "detail": "query"}
    )
    query = plan["best"]["query_ir"]
    assert "dimension.jaffle_customer_first_order_at" in query["group_by"]
    assert "time" not in query
    # With a grain, the draft still groups by that raw timestamp, and "grain" stays flagged.
    intent = "revenue by store and customer first order date at month grain"
    plan = adapter.call_tool("plan", {"intent": intent, "detail": "query"})
    assert "dimension.jaffle_customer_first_order_at" in plan["best"]["query_ir"]["group_by"]
    codes = {w["code"]: w["details"]["terms"] for w in plan["warnings"]}
    assert "grain" in codes["PLAN_UNMATCHED_TERMS"]
