"""Every grouping a ready plan adds traces to the question.

The invariant: a draft is ready only if every grouping it adds traces to the question. A group_by
dimension traces to a grouping the question asks for ("by store", "top 3 stores", "per store",
"for each store"), to the caller's group_by, or to a filter keeping only values the question
names. The time block's grain traces to words outside the question's windows ("by month",
"monthly", "over time"), to the caller's grain, or can't split the rows because the window fits in
one bucket. A ranking keeps the top N of the entity it ranks, never of (entity, period) or
(entity, another dimension). A ranking of the entity split by a period it names asks which
ranking it means, the top N overall or in each period. Every such ranking is held with no
runnable option. The check only holds a plan: it never changes a draft, nor readies one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload, unasked_groupings
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.groupings import _listed_grouping_terms
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.runtime import Runtime
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_listed_groupings import _upkeep

STORE = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER_TIME = "temporal_role.jaffle_order_time"
MONTH = f"{ORDER_TIME}__month"
INCIDENT_ID = "dimension.upkeep_incident_incident_id"
INCIDENT_NAME = "dimension.upkeep_incident_incident_name"
REPORTED_MONTH = "temporal_role.upkeep_incident_reported_at__month"
OK = "ok"
UNASKED = "PLAN_UNASKED_GROUPING"
RANKING = "PLAN_RANKING_PERIOD_AMBIGUOUS"
UNMATCHED = "PLAN_UNMATCHED_TERMS"
GAP = "PLAN_INTENT_COVERAGE_GAP"


@pytest.fixture()
def jaffle(runtime_factory: Any) -> Iterator[Runtime]:
    runtime = runtime_factory("jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture()
def incident(tmp_path: Path) -> Iterator[Runtime]:
    """Two incidents named "Leak", reported in January 2026, costing 10 and 20."""

    runtime = _upkeep(tmp_path / "incident", "incident", "repair")
    try:
        yield runtime
    finally:
        runtime.close()


def _reference(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(runtime.db_path, read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _held(payload: dict[str, Any], code: str) -> dict[str, Any]:
    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == code
    return dict(payload["why"]["details"])


# Revenue by store and month, as SQL; the month is its first day.
_STORE_MONTHS = (
    "SELECT s.store_name AS store, CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month, "
    "SUM(o.order_total_cents / 100.0) AS revenue FROM jaffle_order o "
    "JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY 1, 2"
)


def _cents(rows: list[dict[str, Any]], *fields: str) -> list[tuple[Any, ...]]:
    return [
        (
            *(str(row[field])[:10] if field == MONTH else row[field] for field in fields),
            round(float(row["revenue_usd"]), 2),
        )
        for row in rows
    ]


def test_a_comparison_never_splits_by_a_month_the_question_never_asks_for(
    jaffle: Runtime,
) -> None:
    payload = plan_payload(
        jaffle, intent="food revenue vs drink revenue by store and customer type"
    )

    _held(payload, UNMATCHED)
    # Check the original grain obligation on an explicitly authored store grouping.
    query = {
        **payload["best"]["query_ir"],
        "group_by": [STORE, CUSTOMER_TYPE],
        "order_by": [{"field": "time", "direction": "ASC"}],
    }
    why = unasked_groupings._unasked_grouping_why(jaffle, payload["intent"], query)
    assert why["code"] == UNASKED
    assert why["details"] == {"unasked_groupings": ["month"], "grain": "month"}
    assert query["time"] == {"temporal_role": ORDER_TIME, "grain": "month"}
    assert query["group_by"] == [STORE, CUSTOMER_TYPE]
    reference = sorted(
        (store, kind, round(float(food), 2), round(float(drink), 2))
        for store, kind, food, drink in _reference(
            jaffle,
            "SELECT s.store_name, c.customer_type, SUM(o.food_revenue_cents / 100.0), "
            "SUM(o.drink_revenue_cents / 100.0) FROM jaffle_order o "
            "JOIN jaffle_store s ON o.store_id = s.store_id "
            "JOIN jaffle_customer c ON o.customer_id = c.customer_id GROUP BY 1, 2",
        )
    )
    # Run anyway, the draft splits each store and customer type by month.
    assert len(typed_rows(jaffle.query(query))) > len(reference)
    # Without the time block, as the recovery hint says, the draft answers the question.
    totals = {key: value for key, value in query.items() if key not in {"time", "order_by"}}
    rows = sorted(
        (
            row[STORE],
            row[CUSTOMER_TYPE],
            round(float(row["food_revenue_usd"]), 2),
            round(float(row["drink_revenue_usd"]), 2),
        )
        for row in typed_rows(jaffle.query(totals))
    )
    assert rows == reference


@pytest.mark.parametrize(
    "intent",
    [
        "top 3 stores by revenue at month level",
        "top 1 store by revenue at month level",
        "top 3 stores by monthly revenue",
        "top 3 stores by revenue by month",
    ],
)
def test_a_ranking_split_by_a_period_asks_which_ranking_it_means(
    jaffle: Runtime, intent: str
) -> None:
    limit = 1 if "top 1" in intent else 3
    stores = "store" if limit == 1 else "stores"
    payload = plan_payload(jaffle, intent=intent)

    _held(payload, UNMATCHED)
    query = {**payload["best"]["query_ir"], "group_by": [STORE]}
    why = unasked_groupings._unasked_grouping_why(jaffle, intent, query)
    assert why["code"] == RANKING
    details = why["details"]
    assert details == {"limit": limit, "ranked": [STORE], "grain": "month"}
    assert "clarification" not in details
    assert "execute" not in payload["next"].get("ready_for", [])
    # The message states both readings; no option runs either one.
    assert why["message"].endswith(
        f"The top {limit} {stores} over the whole window, or the top {limit} {stores} in each "
        "month?"
    )
    assert [hint["kind"] for hint in why["recovery_hints"]] == ["ask_which_ranking"]
    assert "query_ir" not in json.dumps(why)

    # The draft keeps the top N store-months, which is neither reading.
    draft = _cents(typed_rows(jaffle.query(query)), STORE, MONTH)
    assert draft == [
        (store, str(month), round(float(revenue), 2))
        for store, month, revenue in _reference(
            jaffle, f"{_STORE_MONTHS} ORDER BY revenue DESC LIMIT {limit}"
        )
    ]


@pytest.mark.parametrize(
    ("intent", "grain"),
    [
        ("which 3 stores have the highest monthly revenue by customer type", {"grain": "month"}),
        ("which 3 stores have the highest revenue by customer type", {}),
    ],
)
def test_a_ranking_of_more_than_its_entity_offers_no_runnable_option(
    jaffle: Runtime, intent: str, grain: dict[str, str]
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    _held(payload, GAP)
    query = {
        **payload["best"]["query_ir"],
        "group_by": [STORE, CUSTOMER_TYPE],
        "order_by": [{"field": "revenue_usd", "direction": "DESC"}],
        "limit": 3,
    }
    why = unasked_groupings._unasked_grouping_why(jaffle, intent, query)
    assert why["code"] == RANKING
    assert why["details"] == {"limit": 3, "ranked": [STORE, CUSTOMER_TYPE], **grain}
    assert "query_ir" not in json.dumps(why)
    # Run anyway, the draft keeps the top 3 (store, customer type) rows, not the top 3 stores.
    assert query["group_by"] == [STORE, CUSTOMER_TYPE]
    stores = [row[STORE] for row in typed_rows(jaffle.query(query))]
    assert stores != [
        store
        for store, _ in _reference(
            jaffle,
            f"SELECT store, SUM(revenue) AS revenue FROM ({_STORE_MONTHS}) GROUP BY 1 "
            "ORDER BY revenue DESC LIMIT 3",
        )
    ]
    assert len(set(stores)) < len(stores)
    if not grain:
        # Said as "top 3 stores by revenue by customer type", the draft drops the customer type
        # and is held for that, with no option either.
        payload = plan_payload(jaffle, intent="top 3 stores by revenue by customer type")
        assert "clarification" not in _held(payload, "PLAN_FALLBACK_SEMANTIC_DRIFT")


def test_a_ranking_of_a_time_axis_value_offers_no_runnable_option(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="top 3 stores by cumulative revenue by month")

    _held(payload, UNMATCHED)
    query = {**payload["best"]["query_ir"], "group_by": [STORE]}
    why = unasked_groupings._unasked_grouping_why(jaffle, payload["intent"], query)
    assert why["code"] == RANKING
    assert why["details"] == {"limit": 3, "ranked": [STORE], "grain": "month"}
    assert "query_ir" not in json.dumps(why)
    # A running total needs its time axis: the draft without its grain doesn't validate.
    assert query["select"][0]["expression"] == {"metric": "metric.sales.cumulative_revenue"}
    totals = {key: value for key, value in query.items() if key not in {"time", "order_by"}}
    assert jaffle.validate(totals)["ok"] is False


# The top 3 stores by revenue, as a draft before its time block.
_TOP_3_STORES = {
    "version": 1,
    "select": [{"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
    "group_by": [STORE],
    "order_by": [{"field": "revenue_usd", "direction": "DESC"}],
    "limit": 3,
}


@pytest.mark.parametrize(
    ("question", "time"),
    [
        # The noun it ranks is the period.
        ("which 3 months had the highest revenue by store", {"grain": "month"}),
        # The months of a window on another calendar.
        (
            "top 3 stores by monthly revenue",
            {
                "grain": "month",
                "start": "2017-01-01",
                "end": "2017-07-01",
                "calendar_id": "fiscal",
                "fill": True,
            },
        ),
    ],
)
def test_a_ranking_of_a_period_or_on_another_calendar_offers_no_runnable_option(
    jaffle: Runtime, question: str, time: dict[str, Any]
) -> None:
    query = {**_TOP_3_STORES, "time": {"temporal_role": ORDER_TIME, **time}}
    assert jaffle.validate(query)["ok"] is True

    why = unasked_groupings._unasked_grouping_why(jaffle, question, query)

    assert why is not None
    assert why["code"] == RANKING
    assert why["details"] == {"limit": 3, "ranked": [STORE], "grain": "month"}
    assert "query_ir" not in json.dumps(why)
    if "months" in question:
        # As planned, "top 3 months by revenue by store" is held with no option too.
        payload = plan_payload(jaffle, intent="top 3 months by revenue by store")
        assert _outcome(payload) != OK
        assert "clarification" not in payload["why"]["details"]


def test_a_ranking_the_question_never_states_is_the_callers(jaffle: Runtime) -> None:
    question = "revenue by store and customer type"
    query = {**_TOP_3_STORES, "group_by": [STORE, CUSTOMER_TYPE]}

    why = unasked_groupings._unasked_grouping_why(jaffle, question, query)

    # The top 3 pairs trace to nothing the question says.
    assert why is not None
    assert why["code"] == RANKING
    assert why["details"] == {"limit": 3, "ranked": [STORE, CUSTOMER_TYPE]}
    assert "query_ir" not in json.dumps(why)
    # The caller's partial_query states them: its limit, over its own group_by.
    caller = {"group_by": [STORE, CUSTOMER_TYPE], "limit": 3}
    assert unasked_groupings._unasked_grouping_why(jaffle, question, query, caller) is None
    # Its limit over a grouping it lacks states another ranking.
    caller = {"group_by": [STORE], "limit": 3}
    why = unasked_groupings._unasked_grouping_why(jaffle, question, query, caller)
    assert why is not None
    assert why["code"] == RANKING


@pytest.mark.parametrize(
    ("intent", "held"),
    [
        ("revenue by store from January 1 2016 to December 31 2017", True),
        (
            "food revenue vs drink revenue by store and customer type from January 1 2016 to "
            "December 31 2017",
            True,
        ),
        # The question asks for the years.
        ("revenue by store from January 1 2016 to December 31 2017 by year", False),
    ],
)
def test_a_window_of_whole_years_never_splits_by_a_year_the_question_never_asks_for(
    jaffle: Runtime, intent: str, held: bool
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    time = payload["best"]["query_ir"]["time"]
    assert time == {
        "temporal_role": ORDER_TIME,
        "grain": "year",
        "start": "2016-01-01",
        "end": "2018-01-01",
    }
    _held(payload, UNMATCHED)
    # The additional grain check still holds an authored store grouping when unasked.
    query = {**payload["best"]["query_ir"], "group_by": [STORE]}
    if "customer type" in intent:
        query["group_by"].append(CUSTOMER_TYPE)
    why = unasked_groupings._unasked_grouping_why(jaffle, intent, query)
    if held:
        assert why["code"] == UNASKED
        assert why["details"] == {"unasked_groupings": ["year"], "grain": "year"}
    else:
        assert why is None


@pytest.mark.parametrize("window", ["last month", "January 2026"])
def test_a_window_inside_the_list_never_hides_a_later_grouping(
    incident: Runtime, window: str
) -> None:
    intent = f"repair cost by incident name, {window} and incident"
    payload = plan_payload(incident, intent=intent)

    assert _listed_grouping_terms(intent, incident._config) == ["incident name", "incident"]
    assert _held(payload, UNMATCHED)["dropped_groupings"] == ["incident"]
    query = payload["best"]["query_ir"]
    assert query["group_by"] == [INCIDENT_NAME]
    # Run anyway, it would add both incidents into one row.
    rows = typed_rows(incident.query({**query, "policy_context": {"now": "2026-02-15"}}))
    assert [(row[INCIDENT_NAME], row["repair_cost"]) for row in rows] == [("Leak", 30)]


def test_a_grouping_after_a_window_keeps_the_window(incident: Runtime, jaffle: Runtime) -> None:
    payload = plan_payload(incident, intent="repair cost by incident name last month and incident")

    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert query["group_by"] == [INCIDENT_NAME, INCIDENT_ID]
    assert query["time"]["range"] == {"last": {"unit": "month", "value": 1}}
    rows = typed_rows(incident.query({**query, "policy_context": {"now": "2026-02-15"}}))
    assert [(row[INCIDENT_ID], row[INCIDENT_NAME], row["repair_cost"]) for row in rows] == [
        (1, "Leak", 10),
        (2, "Leak", 20),
    ]
    # The window filters: neither incident was reported in February.
    assert typed_rows(incident.query({**query, "policy_context": {"now": "2026-03-15"}})) == []

    payload = plan_payload(jaffle, intent="revenue by store last month and customer type")
    _held(payload, "PLAN_FALLBACK_SEMANTIC_DRIFT")
    # The window remains available in the diagnostic draft; author the intended grouping.
    query = {
        **payload["best"]["query_ir"],
        "group_by": [STORE, CUSTOMER_TYPE],
        "order_by": [{"field": "time", "direction": "ASC"}],
    }
    assert query["group_by"] == [STORE, CUSTOMER_TYPE]
    rows = typed_rows(jaffle.query({**query, "policy_context": {"now": "2017-04-15"}}))
    assert sorted(
        (row[STORE], row[CUSTOMER_TYPE], round(float(row["revenue_usd"]), 2)) for row in rows
    ) == sorted(
        (store, kind, round(float(revenue), 2))
        for store, kind, revenue in _reference(
            jaffle,
            "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
            "JOIN jaffle_customer c ON o.customer_id = c.customer_id "
            "WHERE o.ordered_at >= '2017-03-01' AND o.ordered_at < '2017-04-01' GROUP BY 1, 2",
        )
    )


def _draft(query: dict[str, Any]) -> RuntimeCompositionDraft:
    return RuntimeCompositionDraft(
        query={
            "version": 1,
            "select": [
                {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
            ],
            **query,
        },
        resolved=[],
        rationale=[],
        interpreted_intent={},
    )


@pytest.mark.parametrize("path", ["pattern", "fallback"])
def test_every_draft_with_an_unasked_grouping_goes_through_the_one_gate(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    intent = "revenue by store"
    extra = {"group_by": [STORE, CUSTOMER_TYPE]}
    monthly = {"group_by": [STORE], "time": {"temporal_role": ORDER_TIME, "grain": "month"}}
    if path == "pattern":
        monkeypatch.setattr(
            plan_module,
            "compose",
            lambda runtime, text: CompositionResult(
                intent_ir=parse_intent(runtime, text), draft=_draft(extra), pattern="test"
            ),
        )
    else:
        monkeypatch.setattr(
            plan_module,
            "compose",
            lambda runtime, text: CompositionResult(
                intent_ir=parse_intent(runtime, text), draft=None, pattern=""
            ),
        )
        monkeypatch.setattr(
            plan_module,
            "fallback_drafts",
            lambda *args, **kwargs: [(_draft(monthly), "catalog_fallback")],
        )

    payload = plan_payload(jaffle, intent=intent)

    assert payload["best"]["validation_ok"] is True
    if path == "pattern":
        assert _held(payload, UNASKED) == {
            "unasked_groupings": ["Customer type"],
            "dimensions": [CUSTOMER_TYPE],
        }
        # The caller's group_by asks for it.
        payload = plan_payload(
            jaffle, intent=intent, partial_query={"group_by": [STORE, CUSTOMER_TYPE]}
        )
        assert payload["status"] == "ok", payload.get("why")
    else:
        assert payload["best"]["pattern"] == "catalog_fallback"
        assert _held(payload, UNASKED) == {"unasked_groupings": ["month"], "grain": "month"}


@pytest.mark.parametrize(
    ("time", "splits"),
    [
        ({}, False),
        ({"grain": "month"}, True),
        # A window inside one bucket of the grain.
        ({"grain": "quarter", "start": "2017-01-01", "end": "2017-04-01"}, False),
        ({"grain": "year", "start": "2017-01-01", "end": "2017-04-01"}, False),
        ({"grain": "month", "start": "2017-01-01", "end": "2017-04-01"}, True),
        ({"grain": "week", "start": "2017-03-06", "end": "2017-03-13"}, False),
        ({"grain": "week", "start": "2017-03-05", "end": "2017-03-12"}, True),
        ({"grain": "day", "start": "2017-03-15T12:00:00", "end": "2017-03-15T13:00:00"}, False),
        ({"grain": "month", "start": "2016-12-30", "end": "2017-01-03"}, True),
        ({"grain": "month", "start": "2017-01-01"}, True),
        # The last single period of the grain, or one day.
        ({"grain": "month", "range": {"last": {"unit": "month", "value": 1}}}, False),
        ({"grain": "month", "range": {"last": {"unit": "day", "value": 1}}}, False),
        ({"grain": "month", "range": {"last": {"unit": "month", "value": 3}}}, True),
        ({"grain": "month", "range": {"last": {"unit": "week", "value": 1}}}, True),
        ({"grain": "day", "range": {"last": {"unit": "day", "value": 7}}}, True),
        # Another calendar's buckets.
        (
            {"grain": "quarter", "start": "2017-01-01", "end": "2017-02-01", "calendar_id": "fy"},
            True,
        ),
    ],
)
def test_a_grain_splits_the_rows_unless_its_window_fits_one_bucket(
    time: dict[str, Any], splits: bool
) -> None:
    assert unasked_groupings._grain_splits(time) is splits


@dataclass(frozen=True)
class _Case:
    intent: str
    # What plan answered before the check: ready, or the code that held it.
    before: str
    # What it answers now, and the unasked groupings it names.
    after: str = OK
    unasked: tuple[str, ...] = ()


def _moved(intent: str, *unasked: str) -> _Case:
    return _Case(intent, OK, UNASKED, unasked)


def _ranking(intent: str) -> _Case:
    return _Case(intent, OK, RANKING)


_CASES = [
    # A grain the question names, or one bucket.
    _Case("revenue by month", OK),
    _Case("monthly revenue by store", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("show monthly revenue by store", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("weekly revenue last 3 months", OK),
    _Case("daily revenue for the last 30 days", OK),
    _Case("revenue by order date", OK),
    _Case("revenue by order date in March 2017", OK),
    _Case("revenue in Q1 2017 by store", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("revenue last month by store", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("revenue yesterday", OK),
    _Case(
        "revenue by store from January 1 2016 to December 31 2017 by year",
        "ok",
        "PLAN_UNMATCHED_TERMS",
    ),
    _Case("revenue in 2017", OK),
    _Case("top 5 stores by revenue in 2017", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("new customer orders over time", OK),
    _Case("revenue trend over time", OK),
    _Case("revenue vs last month", OK),
    _Case("monthly revenue with YoY", OK),
    _Case("month over month revenue growth by month", OK),
    _Case("revenue by store in each month", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case(
        "revenue vs order count by store last quarter",
        "PLAN_FALLBACK_SEMANTIC_DRIFT",
        "PLAN_FALLBACK_SEMANTIC_DRIFT",
    ),
    _Case("orders by store and month", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("food revenue vs drink revenue in Q1 2017 by store", "ok", "PLAN_UNMATCHED_TERMS"),
    # A dimension the question asks for, or filters to values it names.
    _Case("revenue by store", "PLAN_FALLBACK_SEMANTIC_DRIFT", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Case("top stores by revenue", "PLAN_FALLBACK_SEMANTIC_DRIFT", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Case(
        "which 5 stores had the most orders", "PLAN_INTENT_COVERAGE_GAP", "PLAN_INTENT_COVERAGE_GAP"
    ),
    _Case("revenue per store", "PLAN_FALLBACK_SEMANTIC_DRIFT", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Case("orders per store", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("revenue for each store", "PLAN_UNMATCHED_TERMS", "PLAN_UNMATCHED_TERMS"),
    _Case("revenue for Brooklyn store by month", OK),
    _Case("revenue for Brooklyn and Philadelphia stores by month", OK),
    _Case(
        "revenue by store last month and customer type",
        "PLAN_FALLBACK_SEMANTIC_DRIFT",
        "PLAN_FALLBACK_SEMANTIC_DRIFT",
    ),
    # Held before, for another reason.
    _Case("top 3 stores by revenue in each month", GAP, GAP),
    _Case("revenue by store, last month and customer type", UNMATCHED, UNMATCHED),
    # A ranking split by a period the question names.
    _Case("top 3 stores by revenue at month level", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("top 1 store by revenue at month level", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("top 3 stores by monthly revenue", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("top 3 stores by revenue by month", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("top stores by revenue by month", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("top 3 stores by cumulative revenue by month", "ok", "PLAN_UNMATCHED_TERMS"),
    # A ranking of more than the entity it ranks.
    _Case(
        "which 3 stores have the highest monthly revenue by customer type",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_INTENT_COVERAGE_GAP",
    ),
    _Case(
        "which 3 stores have the highest revenue by customer type",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_INTENT_COVERAGE_GAP",
    ),
    # A month plan picks for a comparison, a year-over-year shift or a qualified ranking.
    _Case("food revenue vs drink revenue by store and customer type", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("food revenue share vs drink revenue share by store", "ok", "PLAN_UNMATCHED_TERMS"),
    _moved("What share of revenue comes from food vs drink?", "month"),
    _moved("orders by customer type, new vs repeat", "month"),
    _Case("revenue vs prior year by store", "ok", "PLAN_UNMATCHED_TERMS"),
    _moved("revenue with YoY", "month"),
    _moved("revenue year over year", "month"),
    _moved("revenue vs last year", "month"),
    _moved("revenue compared to last year", "month"),
    _Case(
        "top 3 stores by revenue with at least 4 distinct customers",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_UNMATCHED_TERMS",
    ),
    _Case(
        "top 3 stores by order count with at least 4 distinct customers",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_UNMATCHED_TERMS",
    ),
    _Case(
        "top stores by order count with at least 4 distinct customers",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_UNMATCHED_TERMS",
    ),
    _Case(
        "top 10 stores by revenue with at least 10 orders",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_UNMATCHED_TERMS",
    ),
    _Case(
        "revenue from customers with at least 10 orders by store",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_UNMATCHED_TERMS",
    ),
    # A window of several periods, split into them.
    _Case(
        "which 3 stores have the highest revenue in the last 6 months",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_INTENT_COVERAGE_GAP",
    ),
    _Case("What is revenue in the last 3 months by store?", "ok", "PLAN_UNMATCHED_TERMS"),
    _Case("revenue for the last 3 months by store", "ok", "PLAN_UNMATCHED_TERMS"),
    _moved("revenue in the last 3 months", "month"),
    _moved("revenue over the past 3 months", "month"),
    _Case("revenue by store over the last 2 weeks", "ok", "PLAN_UNMATCHED_TERMS"),
    _moved("revenue in the trailing 12 months", "month"),
    _moved("revenue trailing 12 months", "month"),
    _moved("Revenue, trailing 7 days", "day"),
    _moved("revenue for the last 30 days", "day"),
    _moved("revenue last 30 days", "day"),
    _moved("revenue for the last 7 days", "day"),
    _moved("revenue last 7 days", "day"),
    _Case("revenue by store last 7 days", "ok", "PLAN_UNMATCHED_TERMS"),
    _moved("orders past 2 weeks", "week"),
    _moved("orders last three quarters", "quarter"),
    _moved("orders from December 30, 2016 to January 2, 2017", "month"),
    _moved("revenue in 2016 and 2017", "year"),
    _moved("revenue between 2016 and 2017", "year"),
    _Case("revenue by store from January 1 2016 to December 31 2017", "ok", "PLAN_UNMATCHED_TERMS"),
    # A store split the question never asks for.
    _Case("new store revenue by month", "PLAN_UNMATCHED_TERMS", "PLAN_UNMATCHED_TERMS"),
    _Case(
        "stores with more than 2000 orders in 2017",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_INTENT_COVERAGE_GAP",
    ),
    _Case(
        "Give me the 28D adoption funnel from signup to Send for stores that have an order rate of over 90% grouped by month",
        "PLAN_INTENT_COVERAGE_GAP",
        "PLAN_UNASKED_GROUPING",
    ),
    _Case("Store name", "PLAN_UNMATCHED_TERMS", "PLAN_UNMATCHED_TERMS"),
    _Case("item revenue where store", "PLAN_UNMATCHED_TERMS", "PLAN_UNMATCHED_TERMS"),
]


def _outcome(payload: dict[str, Any]) -> str:
    if payload["status"] == "ok" and "execute" in payload["next"].get("ready_for", []):
        return OK
    assert "execute" not in payload["next"].get("ready_for", [])
    return str((payload.get("why") or {}).get("code"))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.intent)
def test_the_check_only_holds_a_plan_that_was_ready(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, case: _Case
) -> None:
    jaffle._package_examples = []  # These cases test generic grouping checks.
    after = plan_payload(jaffle, intent=case.intent)
    with monkeypatch.context() as without_checks:
        # The two checks are the only readers of the listed groupings, with the answer-shape
        # check after them, which only holds a plan (test_plan_answer_shape.py).
        without_checks.setattr(plan_module, "_dropped_grouping_why", lambda *args: None)
        without_checks.setattr(plan_module, "_unasked_grouping_why", lambda *args: None)
        without_checks.setattr(plan_module, "_answer_shape_why", lambda *args: None)
        before = plan_payload(jaffle, intent=case.intent)

    # Without them, plan answers as it did before; with them, the draft is the same.
    assert _outcome(before) == case.before
    assert after["best"].get("query_ir") == before["best"].get("query_ir")
    assert after["best"].get("pattern") == before["best"].get("pattern")
    assert _outcome(after) == case.after
    if case.after == case.before:
        assert after.get("why") == before.get("why")
        assert after["next"] == before["next"]
    else:
        # A prior hold can change its reason, but it never gains readiness.
        assert case.after != OK
        details = _held(after, case.after)
        if case.unasked:
            assert details["unasked_groupings"] == list(case.unasked)


def test_a_grain_the_caller_sets_is_asked_for(jaffle: Runtime) -> None:
    caller = {"time": {"temporal_role": ORDER_TIME, "grain": "week"}}
    query = {"group_by": [STORE], "time": {"temporal_role": ORDER_TIME, "grain": "week"}}

    assert (
        unasked_groupings._unasked_grouping_why(jaffle, "revenue by store", query, caller) is None
    )
    why = unasked_groupings._unasked_grouping_why(jaffle, "revenue by store", query)
    assert why is not None
    assert why["details"] == {"unasked_groupings": ["week"], "grain": "week"}
    # The check never changes the draft.
    assert query == {"group_by": [STORE], "time": {"temporal_role": ORDER_TIME, "grain": "week"}}
