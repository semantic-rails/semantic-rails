"""Every grouping a ready plan adds traces to the question.

The invariant: a draft is ready only if every grouping it adds traces to the question. A group_by
dimension traces to a grouping the question asks for ("by store", "top 3 stores", "per store",
"for each store"), to the caller's group_by, or to a filter keeping only values the question
names. The time block's grain traces to words outside the question's windows ("by month",
"monthly", "over time"), to the caller's grain, or can't split the rows because the window fits in
one bucket. A ranking split by a period it names asks which ranking it means: the top N overall
or in each period, never the top N of (entity, period). The check only holds a plan: it never
changes a draft, nor readies one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import RuntimeCompositionDraft, _listed_grouping_terms
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

    assert _held(payload, UNASKED) == {"unasked_groupings": ["month"], "grain": "month"}
    query = payload["best"]["query_ir"]
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
    "intent", ["top 3 stores by revenue at month level", "top 1 store by revenue at month level"]
)
def test_a_ranking_split_by_a_period_asks_which_ranking_it_means(
    jaffle: Runtime, intent: str
) -> None:
    limit = 3 if "3" in intent else 1
    payload = plan_payload(jaffle, intent=intent)

    details = _held(payload, RANKING)
    assert (details["limit"], details["ranked"], details["grain"]) == (limit, [STORE], "month")
    clarification = details["clarification"]
    assert clarification["question"] == (
        f"The top {limit} {'stores' if limit == 3 else 'store'} over the whole window, or the top "
        f"{limit} {'stores' if limit == 3 else 'store'} in each month?"
    )
    overall, per_period = clarification["options"]
    assert (overall["id"], per_period["id"]) == ("top_overall", "top_per_period")

    # The draft keeps the top N store-months, which is neither reading.
    draft = _cents(typed_rows(jaffle.query(payload["best"]["query_ir"])), STORE, MONTH)
    store_months = [
        (store, str(month), round(float(revenue), 2))
        for store, month, revenue in _reference(
            jaffle, f"{_STORE_MONTHS} ORDER BY revenue DESC LIMIT {limit}"
        )
    ]
    assert draft == store_months

    # The top N overall, on their total.
    top = [
        (row[STORE], round(float(row["revenue_usd"]), 2))
        for row in typed_rows(jaffle.query(overall["query_ir"]))
    ]
    assert top == [
        (store, round(float(revenue), 2))
        for store, revenue in _reference(
            jaffle,
            f"SELECT store, SUM(revenue) AS revenue FROM ({_STORE_MONTHS}) GROUP BY 1 "
            f"ORDER BY revenue DESC LIMIT {limit}",
        )
    ]
    # Each of them by month: the breakdown filtered to the stores the ranking returned.
    assert overall["breakdown"]["filter_fields"] == [STORE]
    breakdown = {
        **overall["breakdown"]["query_ir"],
        "where": [{"field": STORE, "op": "in", "value": [store for store, _ in top]}],
    }
    assert _cents(typed_rows(jaffle.query(breakdown)), STORE, MONTH) == [
        (store, str(month), round(float(revenue), 2))
        for store, month, revenue in _reference(
            jaffle,
            f"WITH top AS (SELECT store, SUM(revenue) AS total FROM ({_STORE_MONTHS}) GROUP BY 1 "
            f"ORDER BY total DESC LIMIT {limit}) SELECT * FROM ({_STORE_MONTHS}) "
            "WHERE store IN (SELECT store FROM top) ORDER BY store, month",
        )
    ]

    # The top N in each month: each month's first N rows.
    kept: list[tuple[Any, ...]] = []
    seen: Counter[str] = Counter()
    for row in _cents(typed_rows(jaffle.query(per_period["query_ir"])), STORE, MONTH):
        if seen[row[1]] < per_period["keep_first_per_period"]:
            seen[row[1]] += 1
            kept.append(row)
    assert per_period["keep_first_per_period"] == limit
    assert kept == [
        (store, str(month), round(float(revenue), 2))
        for store, month, revenue in _reference(
            jaffle,
            f"SELECT * FROM ({_STORE_MONTHS}) QUALIFY ROW_NUMBER() OVER "
            f"(PARTITION BY month ORDER BY revenue DESC, store) <= {limit} "
            "ORDER BY month, revenue DESC, store",
        )
    ]
    if limit == 1:
        # The readings differ: Philadelphia overall, but Brooklyn in July and August 2017,
        # and the draft's one store-month is Brooklyn's August.
        assert [store for store, _ in top] == ["Philadelphia"]
        assert [(store, month) for store, month, _ in kept if store == "Brooklyn"] == [
            ("Brooklyn", "2017-07-01"),
            ("Brooklyn", "2017-08-01"),
        ]
        assert [row[:2] for row in draft] == [("Brooklyn", "2017-08-01")]


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
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
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
            "version": 2,
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
    assert plan_module._grain_splits(time) is splits


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
    _Case("monthly revenue by store", OK),
    _Case("show monthly revenue by store", OK),
    _Case("weekly revenue last 3 months", OK),
    _Case("daily revenue for the last 30 days", OK),
    _Case("revenue by order date", OK),
    _Case("revenue by order date in March 2017", OK),
    _Case("revenue in Q1 2017 by store", OK),
    _Case("revenue last month by store", OK),
    _Case("revenue yesterday", OK),
    _Case("revenue in 2016 and 2017", OK),
    _Case("revenue in 2017", OK),
    _Case("top 5 stores by revenue in 2017", OK),
    _Case("new customer orders over time", OK),
    _Case("revenue trend over time", OK),
    _Case("revenue vs last month", OK),
    _Case("monthly revenue with YoY", OK),
    _Case("month over month revenue growth by month", OK),
    _Case("revenue by store in each month", OK),
    _Case("revenue vs order count by store last quarter", OK),
    _Case("orders by store and month", OK),
    _Case("food revenue vs drink revenue in Q1 2017 by store", OK),
    # A dimension the question asks for, or filters to values it names.
    _Case("revenue by store", OK),
    _Case("top stores by revenue", OK),
    _Case("which 5 stores had the most orders", OK),
    _Case("revenue per store", OK),
    _Case("orders per store", OK),
    _Case("revenue for each store", OK),
    _Case("revenue for Brooklyn store by month", OK),
    _Case("revenue for Brooklyn and Philadelphia stores by month", OK),
    _Case("revenue by store last month and customer type", OK),
    # Held before, for another reason.
    _Case("top 3 stores by revenue in each month", GAP, GAP),
    _Case("revenue by store, last month and customer type", UNMATCHED, UNMATCHED),
    # A ranking split by a period the question names.
    _ranking("top 3 stores by revenue at month level"),
    _ranking("top 1 store by revenue at month level"),
    _ranking("top 3 stores by monthly revenue"),
    _ranking("top 3 stores by revenue by month"),
    _ranking("top stores by revenue by month"),
    # A month plan picks for a comparison, a year-over-year shift or a qualified ranking.
    _moved("food revenue vs drink revenue by store and customer type", "month"),
    _moved("food revenue share vs drink revenue share by store", "month"),
    _moved("What share of revenue comes from food vs drink?", "month"),
    _moved("orders by customer type, new vs repeat", "month"),
    _moved("revenue vs prior year by store", "month"),
    _moved("revenue with YoY", "month"),
    _moved("revenue year over year", "month"),
    _moved("revenue vs last year", "month"),
    _moved("revenue compared to last year", "month"),
    _moved("top 3 stores by revenue with at least 4 distinct customers", "month"),
    _moved("top 3 stores by order count with at least 4 distinct customers", "month"),
    _moved("top stores by order count with at least 4 distinct customers", "month"),
    _moved("top 10 stores by revenue with at least 10 orders", "month"),
    _moved("revenue from customers with at least 10 orders by store", "month"),
    # A window of several periods, split into them.
    _moved("which 3 stores have the highest revenue in the last 6 months", "month"),
    _moved("What is revenue in the last 3 months by store?", "month"),
    _moved("revenue for the last 3 months by store", "month"),
    _moved("revenue in the last 3 months", "month"),
    _moved("revenue over the past 3 months", "month"),
    _moved("revenue by store over the last 2 weeks", "week"),
    _moved("revenue in the trailing 12 months", "month"),
    _moved("revenue trailing 12 months", "month"),
    _moved("Revenue, trailing 7 days", "day"),
    _moved("revenue for the last 30 days", "day"),
    _moved("revenue last 30 days", "day"),
    _moved("revenue for the last 7 days", "day"),
    _moved("revenue last 7 days", "day"),
    _moved("revenue by store last 7 days", "day"),
    _moved("orders past 2 weeks", "week"),
    _moved("orders last three quarters", "quarter"),
    _moved("orders from December 30, 2016 to January 2, 2017", "month"),
    # A store split the question never asks for.
    _moved("new store revenue by month", "Store name"),
    _moved("stores with more than 2000 orders in 2017", "Store name"),
    _moved(
        "Give me the 28D adoption funnel from signup to Send for stores that have an order rate "
        "of over 90% grouped by month",
        "Store name",
    ),
    _moved("Store name", "Store name"),
    _moved("item revenue where store", "Store name"),
]


def _outcome(payload: dict[str, Any]) -> str:
    if payload["status"] == "ok" and "execute" in payload["next"].get("ready_for", []):
        return OK
    return str((payload.get("why") or {}).get("code"))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.intent)
def test_the_check_only_holds_a_plan_that_was_ready(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, case: _Case
) -> None:
    after = plan_payload(jaffle, intent=case.intent)
    with monkeypatch.context() as without_checks:
        # The two checks are the only readers of the listed groupings.
        without_checks.setattr(plan_module, "_dropped_grouping_why", lambda *args: None)
        without_checks.setattr(plan_module, "_unasked_grouping_why", lambda *args: None)
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
        # Only a plan that was ready is held.
        assert case.before == OK
        details = _held(after, case.after)
        if case.unasked:
            assert details["unasked_groupings"] == list(case.unasked)


def test_a_grain_the_caller_sets_is_asked_for(jaffle: Runtime) -> None:
    caller = {"time": {"temporal_role": ORDER_TIME, "grain": "week"}}
    query = {"group_by": [STORE], "time": {"temporal_role": ORDER_TIME, "grain": "week"}}

    assert plan_module._unasked_grouping_why(jaffle, "revenue by store", query, caller) is None
    why = plan_module._unasked_grouping_why(jaffle, "revenue by store", query)
    assert why is not None
    assert why["details"] == {"unasked_groupings": ["week"], "grain": "week"}
    # The check never changes the draft.
    assert query == {"group_by": [STORE], "time": {"temporal_role": ORDER_TIME, "grain": "week"}}


def test_a_ranking_clarification_keeps_the_window(jaffle: Runtime) -> None:
    query = {
        "version": 2,
        "select": [{"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
        "time": {
            "temporal_role": ORDER_TIME,
            "grain": "month",
            "start": "2017-01-01",
            "end": "2017-07-01",
        },
        "group_by": [STORE],
        "order_by": [{"field": "revenue_usd", "direction": "DESC"}],
        "limit": 1,
    }
    why = plan_module._unasked_grouping_why(jaffle, "top store by monthly revenue", query)

    assert why is not None
    overall, per_period = why["details"]["clarification"]["options"]
    window = {"start": "2017-01-01", "end": "2017-07-01"}
    assert overall["query_ir"]["time"] == {"temporal_role": ORDER_TIME, **window}
    assert overall["query_ir"]["limit"] == 1
    assert per_period["query_ir"]["time"] == query["time"]
    assert "limit" not in per_period["query_ir"]
    assert per_period["query_ir"]["order_by"] == [
        {"field": "time", "direction": "ASC"},
        {"field": "revenue_usd", "direction": "DESC"},
        {"field": STORE, "direction": "ASC"},
    ]
    # Philadelphia, over the first half of 2017.
    assert [row[STORE] for row in typed_rows(jaffle.query(overall["query_ir"]))] == ["Philadelphia"]
