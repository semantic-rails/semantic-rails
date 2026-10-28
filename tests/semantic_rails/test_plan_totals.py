"""plan drafts the total a question asks for: a grand total row, subtotals, each group's total
beside its rows, or a share of the total; and holds the draft, saying why, when the engine
refuses the total or the draft has no place for it (``planner/totals.py``)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner.totals import asked_totals
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.planner_fixtures import jaffle_runtime

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)

REVENUE = "measure.jaffle.revenue_usd"
STORE_ID = "dimension.jaffle_store_id"
STORE = "dimension.jaffle_store_name"
STORES = [STORE_ID, STORE]
NOW = "2018-01-01T00:00:00Z"


def _plan(runtime: Runtime, question: str, **kwargs: Any) -> dict[str, Any]:
    return plan_payload(runtime, intent=question, detail="query", now=NOW, **kwargs)


def _ready(plan: dict[str, Any]) -> dict[str, Any]:
    assert plan["status"] == "ok", plan.get("why")
    assert not plan.get("warnings"), plan["warnings"]
    return dict(plan["best"]["query"])


def _items(query: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in query["select"] if isinstance(item, dict)]


@pytest.mark.parametrize(
    "question,kind,words,rest",
    [
        ("revenue by store with the grand total", "rows", "", "revenue by store"),
        ("revenue by store, plus a total row", "rows", "", "revenue by store"),
        ("grand total revenue last year", "rows", "", "revenue last year"),
        (
            "revenue by store and channel with subtotals by store and a grand total",
            "subtotals",
            "store",
            "revenue by store and channel",
        ),
        (
            "orders by store and type with totals by store",
            "subtotals",
            "store",
            "orders by store and type",
        ),
        (
            "revenue by store, channel and type with subtotals by store and channel",
            "subtotals",
            "store and channel",
            "revenue by store, channel and type",
        ),
        (
            "orders by store and month, with each store's total across months",
            "beside",
            "store",
            "orders by store and month",
        ),
        ("revenue total for each store", "beside", "store", "revenue by store"),
        ("each store's share of revenue", "share", "store", "revenue by store"),
        ("revenue by store as a share of the total", "share", "", "revenue by store"),
        (
            "what percent of total revenue comes from each store",
            "share",
            "",
            "what revenue comes from each store",
        ),
    ],
)
def test_a_question_s_total_words_are_read_apart_from_the_rest(
    runtime, question, kind, words, rest
):
    asked = asked_totals(question, runtime._config)
    assert asked is not None
    assert (asked.kind, asked.words, asked.rest) == (kind, words, rest)


@pytest.mark.parametrize(
    "question",
    [
        "total revenue by store",
        "revenue and total orders by store",
        "revenue by store with total revenue",
        "food revenue share by store",
        "grand total",
        # "Each store's percentage of returning customers" may be a rate inside each store.
        "each store's percentage of revenue",
    ],
)
def test_a_total_that_is_a_measure_s_own_word_asks_for_no_total(runtime, question):
    assert asked_totals(question, runtime._config) is None


def test_a_catalog_name_holding_a_total_s_word_is_read_as_that_object():
    """ "Order total for each store" asks for the Order total measure by store, never for a
    total of orders beside each store's rows; the metric's own words don't stop other phrases."""
    order_total = SimpleNamespace(label="Order total", name="order_total", aliases=["basket"])
    config = SimpleNamespace(measures=[order_total], metric_recipes=[], dimensions=[], segments=[])
    assert asked_totals("order total for each store", config) is None
    assert asked_totals("order totals by store with the grand total", config) is None
    asked = asked_totals("revenue by store with the grand total", config)
    assert asked is not None and asked.kind == "rows"


def test_the_grand_total_is_a_total_row(runtime):
    query = _ready(_plan(runtime, "revenue by store with the grand total"))
    assert query["totals"] == [] and query["group_by"] == STORES
    rows = runtime.query(query, now=NOW)["rows"]
    total = [row for row in rows if row["total_over"]]
    stores = [row for row in rows if not row["total_over"]]
    assert len(total) == 1 and len(stores) == 2
    assert Decimal(str(total[0]["revenue_usd"])) == pytest.approx(
        sum(Decimal(str(row["revenue_usd"])) for row in stores)
    )


def test_the_plan_is_of_the_question_as_asked(runtime):
    plan = plan_payload(runtime, intent="revenue by store with the grand total", now=NOW)
    assert plan["intent"] == "revenue by store with the grand total"
    assert plan["intent_ir"]["intent"] == "revenue by store"
    assert plan["intent_ir"]["totals"] == {"kind": "rows", "phrase": "with the grand total"}
    assert plan["next"]["ready_for"] == ["execute"]


def test_subtotals_keep_the_named_grouping_by_its_name(runtime):
    """A level keeps the key of each name it keeps, so the store's name alone is the level:
    naming the key too would add a level of each store's rows again, without its name."""
    query = _ready(_plan(runtime, "revenue by store and customer type with subtotals by store"))
    assert query["group_by"] == [*STORES, "dimension.jaffle_customer_type"]
    assert query["totals"] == [STORE]
    rows = runtime.query(query, now=NOW)["rows"]
    subtotals = [row for row in rows if row["total_over"] and row[STORE]]
    assert sorted(row[STORE] for row in subtotals) == ["Brooklyn", "Philadelphia"]
    for subtotal in subtotals:
        own = [row for row in rows if not row["total_over"] and row[STORE] == subtotal[STORE]]
        assert subtotal["revenue_usd"] == pytest.approx(sum(row["revenue_usd"] for row in own))
    assert len([row for row in rows if row["total_over"] and not row[STORE]]) == 1


def test_subtotals_of_a_series_s_one_grouping_are_its_total_across_the_periods(runtime):
    """Total rows keep the period, so each store's total across the months sits beside them."""
    query = _ready(_plan(runtime, "revenue by store and month with subtotals by store"))
    assert "totals" not in query
    [item] = _items(query)
    assert item["expression"] == {"kind": "total", "input": REVENUE, "over": ["period"]}


@pytest.mark.parametrize(
    "question,over",
    [
        ("order count by store and month with each store's total across months", ["period"]),
        ("revenue by store and month with each month's total", STORES),
        (
            "revenue by store and customer type with each store's total",
            ["dimension.jaffle_customer_type"],
        ),
        # With nothing named after it, a group's total adds up everything else, the months too.
        (
            "monthly revenue by store and customer type with each store's total",
            ["dimension.jaffle_customer_type", "period"],
        ),
        # What it adds up across, when named, is all it adds up across.
        (
            "monthly revenue by store and customer type with each store's total across types",
            ["dimension.jaffle_customer_type"],
        ),
        (
            "monthly revenue by store and customer type with each month's total across stores",
            STORES,
        ),
        (
            "monthly revenue by store and customer type with each store's total across types "
            "and months",
            ["dimension.jaffle_customer_type", "period"],
        ),
        # Without a grouping, each month's row is its own total.
        ("revenue by month with each month's total", None),
    ],
)
def test_each_group_s_total_sits_beside_its_rows(runtime, question, over):
    query = _ready(_plan(runtime, question))
    if over is None:
        assert not _items(query)
        return
    [item] = _items(query)
    assert item["expression"]["kind"] == "total" and item["expression"]["over"] == over
    # The draft's own order_by on its keys runs beside the total's coarser call.
    assert query["order_by"] and runtime.query(query, now=NOW)["rows"]


def test_each_group_s_share_among_the_filtered_rows_is_ready(runtime):
    """Without "of the total", the filter names the rows the share is among."""
    query = _ready(_plan(runtime, "each store's share of revenue for new customers"))
    assert query["where"] and _items(query)[0]["expression"]["kind"] == "share"


def test_a_share_of_a_series_grand_total_is_over_the_periods_too(runtime):
    query = _ready(_plan(runtime, "revenue by store and month as a share of the grand total"))
    [item] = _items(query)
    assert item["expression"]["over"] == [*STORES, "period"]


def test_an_example_authored_for_the_whole_question_answers_it(runtime, monkeypatch):
    """A package example answers only its own question, total words included."""
    example = {
        "question": "revenue by store with the grand total",
        "query": {"select": ["measure.jaffle.order_count"], "group_by": [STORE]},
    }
    monkeypatch.setattr(runtime, "_get_package_examples", lambda: [("example.totals", example)])
    plan = _plan(runtime, "Revenue by store with the grand total")
    assert plan["best"]["pattern"] == "package_example"
    assert plan["best"]["query"] == example["query"]


def test_each_period_s_share_is_over_the_periods(runtime):
    query = _ready(_plan(runtime, "each month's share of revenue"))
    [item] = _items(query)
    assert item["expression"] == {"kind": "share", "input": REVENUE, "over": ["period"]}
    rows = runtime.query(query, now=NOW)["rows"]
    assert sum(row["revenue_usd_share"] for row in rows) == pytest.approx(1)


def test_a_total_row_beside_a_ranking_says_it_adds_up_every_row(runtime):
    """ "Top 3 stores with the grand total": the total of every store the call keeps, before
    its limit, as the answer's TOTALS says too."""
    plan = _plan(runtime, "top 3 stores by revenue with the grand total")
    query = _ready(plan)
    assert query["totals"] == [] and query["limit"] == 3
    assert any("before its having and limit" in line for line in plan["assumptions"])


def test_each_group_s_share_is_of_the_total_across_the_named_grouping(runtime):
    query = _ready(_plan(runtime, "each store's share of revenue and orders"))
    assert [item["expression"] for item in _items(query)] == [
        {"kind": "share", "input": REVENUE, "over": STORES},
        {"kind": "share", "input": "measure.jaffle.order_count", "over": STORES},
    ]
    rows = runtime.query(query, now=NOW)["rows"]
    assert sum(row["revenue_usd_share"] for row in rows) == pytest.approx(1)


@pytest.mark.parametrize(
    "question,code",
    [
        # A share adds up its input across the rows; an average doesn't add up.
        ("each store's share of average order value", "ROLLUP_UNSAFE"),
        # No total row is one number across a series' periods.
        ("revenue by store and month with the grand total", None),
        ("revenue by month with the grand total", None),
        ("revenue by store with subtotals by product", None),
        # "Over the year" names none of a monthly call's other groupings.
        ("revenue by store and month with each store's total over the year", None),
        ("revenue by store and customer type with each store's total across months", None),
        # Subtotal levels end at a grand total, which a series' total rows aren't.
        ("revenue by store and month with subtotals by store and a grand total", None),
        (
            "monthly revenue by store and customer type with subtotals by store and a grand total",
            None,
        ),
        # "Of total revenue" may mean every row; the share's total counts the filtered ones.
        ("share of total revenue by store for new customers", None),
        # "Customer" names the customer, never "Customer type".
        ("revenue by customer type and store with each customer's total", None),
        # A series' "total": each period's, or the whole window's?
        ("revenue by store and month as a share of the total", None),
    ],
)
def test_a_total_the_draft_cannot_carry_holds_the_plan(runtime, question, code):
    plan = _plan(runtime, question)
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "PLAN_TOTALS_NOT_DRAFTED"
    assert plan["why"]["details"].get("refusal", {}).get("code") == code
    assert [row["code"] for row in plan["warnings"]] == ["PLAN_TOTALS_NOT_DRAFTED"]
    query = plan["best"]["query"]
    assert "totals" not in query and not _items(query)
    assert runtime.validate(query, now=NOW)["ok"]


@pytest.fixture(scope="module")
def per_store_revenue(runtime: Runtime) -> Iterator[Runtime]:
    """Revenue may only be read grouped by store."""
    policy = SemanticPolicyConfig(
        id="policy.test.revenue_per_store",
        kind="metric_constraint",
        object_ids=[REVENUE],
        config={"required_group_by": [STORE]},
    )
    config = runtime.package_config
    with closing(
        Runtime.from_config(
            replace(config, semantic_policies=[*config.semantic_policies, policy]),
            source_path=runtime.source_path,
        )
    ) as governed:
        yield governed


def test_a_total_a_policy_refuses_is_a_warning_on_the_draft_without_it(per_store_revenue):
    """The grand-total row reads revenue grouped by nothing, which the policy forbids: plan
    hands back the call without it, held, never one execute refuses."""
    plan = plan_payload(per_store_revenue, intent="revenue by store with the grand total", now=NOW)
    assert plan["status"] == "low_confidence"
    assert "ready_for" not in plan["next"]
    why = plan["why"]
    assert why["code"] == "PLAN_TOTALS_NOT_DRAFTED"
    assert why["details"]["refusal"]["code"] == "POLICY_DENIED"
    assert "plan doesn't call it ready" in why["message"]
    assert "totals" not in plan["best"]["query"]
    assert per_store_revenue.validate(plan["best"]["query"], now=NOW)["ok"]
    # Each store's total across months stays grouped by store: the policy allows it.
    beside = _plan(
        per_store_revenue, "revenue by store and month with each store's total across months"
    )
    assert _items(_ready(beside))[0]["expression"]["over"] == ["period"]


def test_withheld_values_refuse_a_total_beside_their_rank(runtime):
    """A caller who may only rank stores by revenue can't read its grand total."""
    policy = SemanticPolicyConfig(
        id="policy.test.rank_only",
        kind="object_access",
        object_ids=[REVENUE],
        action="withhold_values",
        roles=["sales"],
    )
    config = runtime.package_config
    rank = {
        "select": [{"expression": REVENUE, "as": "revenue"}],
        "group_by": [STORE],
        "order_by": [
            {"field": "revenue", "direction": "desc"},
            {"field": STORE, "direction": "desc"},
        ],
        "limit": 2,
        "policy_context": {"roles": ["sales"]},
    }
    with closing(
        Runtime.from_config(
            replace(config, semantic_policies=[*config.semantic_policies, policy]),
            source_path=runtime.source_path,
        )
    ) as withheld:
        assert _plan(withheld, "top 2 stores by revenue", partial_query=rank)["status"] == "ok"
        plan = _plan(withheld, "top 2 stores by revenue with the grand total", partial_query=rank)
    assert plan["why"]["code"] == "PLAN_TOTALS_NOT_DRAFTED"
    assert plan["why"]["details"]["refusal"]["code"] == "POLICY_DENIED"


def test_a_draft_held_for_something_else_says_it_carries_no_total(runtime):
    """Two totals in one call: the plan's own hold stays, and the warning says why no total."""
    plan = _plan(runtime, "revenue by store with the grand total and each store's share")
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] != "PLAN_TOTALS_NOT_DRAFTED"
    assert "PLAN_TOTALS_NOT_DRAFTED" in [row["code"] for row in plan["warnings"]]
