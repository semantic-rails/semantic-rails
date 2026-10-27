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
from contextlib import closing
from decimal import Decimal
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload, unasked_groupings
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.call import Call, time_of
from semantic_rails.planner.groupings import _listed_grouping_terms
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.planner_fixtures import jaffle_runtime
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_listed_groupings import _outcome, _upkeep

STORE = "dimension.jaffle_store_name"
STORE_ID = "dimension.jaffle_store_id"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER_TIME = "temporal_role.jaffle_order_time"
MONTH = "period"
INCIDENT_ID = "dimension.upkeep_incident_incident_id"
INCIDENT_NAME = "dimension.upkeep_incident_incident_name"
REPORTED_MONTH = "period"
OK = "ok"
UNASKED = "PLAN_UNASKED_GROUPING"
RANKING = "PLAN_RANKING_PERIOD_AMBIGUOUS"
UNMATCHED = "PLAN_UNMATCHED_TERMS"
GAP = "PLAN_INTENT_COVERAGE_GAP"


jaffle = pytest.fixture(scope="module", name="jaffle")(jaffle_runtime)


@pytest.fixture(scope="module")
def incident(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """Two incidents named "Leak", reported in January 2026, costing 10 and 20."""
    path = tmp_path_factory.mktemp("upkeep") / "incident"
    with closing(opened(_upkeep(path, "incident", "repair"))) as runtime:
        yield runtime


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

    # No grain is asked for, so none is drafted; "vs" then names nothing the draft compares,
    # and the coverage check holds it for that.
    details = _held(payload, GAP)
    assert [gap["kind"] for gap in details["gaps"]] == ["comparison_unrealized"]
    query = payload["best"]["query"]
    assert "time" not in query
    assert query["group_by"] == [STORE_ID, STORE, CUSTOMER_TYPE]
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
    # Run anyway, the draft is each store and customer type's two totals, never split by month.
    rows = sorted(
        (
            row[STORE],
            row[CUSTOMER_TYPE],
            round(float(row["food_revenue_usd"]), 2),
            round(float(row["drink_revenue_usd"]), 2),
        )
        for row in typed_rows(jaffle.query(query))
    )
    assert rows == reference


@pytest.mark.parametrize(
    "intent",
    [
        "top 3 stores by revenue at month level",
        "top 1 store by revenue at month level",
        "top 3 stores by revenue by month",
    ],
)
def test_a_ranking_split_by_a_period_asks_which_ranking_it_means(
    jaffle: Runtime, intent: str
) -> None:
    limit = 1 if "top 1" in intent else 3
    payload = plan_payload(jaffle, intent=intent)

    details = _held(payload, RANKING)
    assert details == {"limit": limit, "ranked": [STORE_ID, STORE], "grain": "month"}
    assert "clarification" not in details
    assert "execute" not in payload["next"].get("ready_for", [])
    # It asks which of the two readings is meant; no option runs either one.
    assert [hint["kind"] for hint in payload["why"]["recovery_hints"]] == ["ask_which_ranking"]
    assert "query" not in json.dumps(payload["why"])

    # The draft keeps the top N store-months, which is neither reading.
    draft = _cents(typed_rows(jaffle.query(payload["best"]["query"])), STORE, MONTH)
    assert draft == [
        (store, str(month), round(float(revenue), 2))
        for store, month, revenue in _reference(
            jaffle, f"{_STORE_MONTHS} ORDER BY revenue DESC LIMIT {limit}"
        )
    ]


@pytest.mark.parametrize(
    ("intent", "grain"),
    [
        ("which 3 stores have the highest revenue by customer type by month", {"grain": "month"}),
        ("which 3 stores have the highest revenue by customer type", {}),
    ],
)
def test_a_ranking_of_more_than_its_entity_offers_no_runnable_option(
    jaffle: Runtime, intent: str, grain: dict[str, str]
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    ranked = [CUSTOMER_TYPE, STORE_ID, STORE]
    assert _held(payload, RANKING) == {"limit": 3, "ranked": ranked, **grain}
    assert "query" not in json.dumps(payload["why"])
    # Run anyway, the draft keeps the top 3 (store, customer type) rows, not the top 3 stores.
    query = payload["best"]["query"]
    assert query["group_by"] == ranked
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
        assert "clarification" not in _held(payload, UNMATCHED)


def test_a_ranking_of_a_time_axis_value_offers_no_runnable_option(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="top 3 stores by cumulative revenue by month")

    assert _held(payload, RANKING) == {"limit": 3, "ranked": [STORE_ID, STORE], "grain": "month"}
    assert "query" not in json.dumps(payload["why"])
    # A running total needs its time axis: the draft without its grain doesn't validate.
    query = payload["best"]["query"]
    assert query["select"][0] == "metric.sales.cumulative_revenue"
    totals = {key: value for key, value in query.items() if key not in {"time", "order_by"}}
    assert jaffle.validate(totals)["ok"] is False


# The top 3 stores by revenue, as a draft before its time block.
_TOP_3_STORES = {
    "select": ["measure.jaffle.revenue_usd"],
    "group_by": [STORE],
    "order_by": [{"field": "revenue_usd", "direction": "desc"}],
    "limit": 3,
}


@pytest.mark.parametrize(
    ("question", "time", "valid"),
    [
        # The noun it ranks is the period.
        ("which 3 months had the highest revenue by store", {"grain": "month"}, True),
        # The months of a window on another calendar (refused in this release).
        (
            "top 3 stores by monthly revenue",
            {
                "grain": "month",
                "start": "2017-01-01",
                "end": "2017-07-01",
                "calendar_id": "fiscal",
                "fill": True,
            },
            False,
        ),
    ],
)
def test_a_ranking_of_a_period_or_on_another_calendar_offers_no_runnable_option(
    jaffle: Runtime, question: str, time: dict[str, Any], valid: bool
) -> None:
    query = {**_TOP_3_STORES, "time": {"temporal_role": ORDER_TIME, **time}}
    assert jaffle.validate(query)["ok"] is valid

    why = unasked_groupings._unasked_grouping_why(jaffle, question, Call.from_call(query))

    assert why is not None
    assert why["code"] == RANKING
    assert why["details"] == {"limit": 3, "ranked": [STORE], "grain": "month"}
    assert "query" not in json.dumps(why)
    if "months" in question:
        # As planned, "top 3 months by revenue by store" is held with no option too.
        payload = plan_payload(jaffle, intent="top 3 months by revenue by store")
        assert _outcome(payload) != OK
        assert "clarification" not in payload["why"]["details"]


def test_a_ranking_the_question_never_states_is_the_callers(jaffle: Runtime) -> None:
    question = "revenue by store and customer type"
    query = {**_TOP_3_STORES, "group_by": [STORE, CUSTOMER_TYPE]}

    why = unasked_groupings._unasked_grouping_why(jaffle, question, Call.from_call(query))

    # The top 3 pairs trace to nothing the question says.
    assert why is not None
    assert why["code"] == RANKING
    assert why["details"] == {"limit": 3, "ranked": [STORE, CUSTOMER_TYPE]}
    assert "query" not in json.dumps(why)
    # The caller's partial_query states them: its limit, over its own group_by.
    caller = {"group_by": [STORE, CUSTOMER_TYPE], "limit": 3}
    assert (
        unasked_groupings._unasked_grouping_why(
            jaffle, question, Call.from_call(query), Call.from_call(caller)
        )
        is None
    )
    # Its limit over a grouping it lacks states another ranking.
    caller = {"group_by": [STORE], "limit": 3}
    why = unasked_groupings._unasked_grouping_why(
        jaffle, question, Call.from_call(query), Call.from_call(caller)
    )
    assert why is not None
    assert why["code"] == RANKING


@pytest.mark.parametrize(
    ("intent", "grain", "code"),
    [
        # One total per store over both years (its rows: test_a_one_total_matches_plain_sql).
        ("revenue by store from January 1 2016 to December 31 2017", None, OK),
        # Unsplit, "vs" names nothing to compare with, so the draft is held for that.
        (
            "food revenue vs drink revenue by store and customer type from January 1 2016 to "
            "December 31 2017",
            None,
            GAP,
        ),
        # The question asks for the years.
        ("revenue by store from January 1 2016 to December 31 2017 by year", "year", OK),
    ],
)
def test_a_window_of_whole_years_never_splits_by_a_year_the_question_never_asks_for(
    jaffle: Runtime, intent: str, grain: str | None, code: str
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    time = payload["best"]["query"]["time"]
    assert time == {
        "temporal_role": ORDER_TIME,
        **({"grain": grain} if grain else {}),
        "start": "2016-01-01",
        "end": "2018-01-01",
    }
    if code == OK:
        assert payload["status"] == "ok", payload.get("why")
        assert "execute" in payload["next"]["ready_for"]
    else:
        details = _held(payload, code)
        assert [gap["kind"] for gap in details["gaps"]] == ["comparison_unrealized"]


@pytest.mark.parametrize("window", ["last month", "January 2026"])
def test_a_window_inside_the_list_never_hides_a_later_grouping(
    incident: Runtime, window: str
) -> None:
    intent = f"repair cost by incident name, {window} and incident"
    payload = plan_payload(incident, intent=intent)

    assert _listed_grouping_terms(intent, incident._config) == ["incident name", "incident"]
    assert _held(payload, UNMATCHED)["dropped_groupings"] == ["incident"]
    query = payload["best"]["query"]
    assert query["group_by"] == [INCIDENT_NAME]
    # Run anyway, it would add both incidents into one row.
    rows = typed_rows(incident.query(query, now="2026-02-15"))
    assert [(row[INCIDENT_NAME], row["repair_cost"]) for row in rows] == [("Leak", 30)]


def test_a_grouping_after_a_window_keeps_the_window(incident: Runtime) -> None:
    payload = plan_payload(incident, intent="repair cost by incident name last month and incident")

    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    assert query["group_by"] == [INCIDENT_NAME, INCIDENT_ID]
    assert query["time"]["last"] == {"unit": "month", "value": 1}
    rows = typed_rows(incident.query(query, now="2026-02-15"))
    assert [(row[INCIDENT_ID], row[INCIDENT_NAME], row["repair_cost"]) for row in rows] == [
        (1, "Leak", 10),
        (2, "Leak", 20),
    ]
    # The window filters: neither incident was reported in February.
    assert typed_rows(incident.query(query, now="2026-03-15")) == []


def _draft(query: dict[str, Any]) -> RuntimeCompositionDraft:
    return RuntimeCompositionDraft(
        query=Call.from_call({"select": ["measure.jaffle.revenue_usd"], **query}),
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
        ({"grain": "month", "last": {"unit": "month", "value": 1}}, False),
        ({"grain": "month", "last": {"unit": "day", "value": 1}}, False),
        ({"grain": "month", "last": {"unit": "month", "value": 3}}, True),
        ({"grain": "month", "last": {"unit": "week", "value": 1}}, True),
        ({"grain": "day", "last": {"unit": "day", "value": 7}}, True),
    ],
)
def test_a_grain_splits_the_rows_unless_its_window_fits_one_bucket(
    time: dict[str, Any], splits: bool
) -> None:
    assert unasked_groupings._grain_splits(time_of(time)) is splits


# "last week" is 2017-08-14 .. 2017-08-21: each relative window is whole periods before now.
AUGUST = "2017-08-21"
_STORE_REVENUE = (
    "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0) FROM jaffle_order o "
    "JOIN jaffle_store s ON o.store_id = s.store_id"
)
_WINDOW = " WHERE o.ordered_at >= TIMESTAMP '{}' AND o.ordered_at < TIMESTAMP '{}'"
_TOTAL = "SELECT {} FROM jaffle_order o" + _WINDOW
REVENUE = "SUM(o.order_total_cents / 100.0)"
ORDERS = "COUNT(DISTINCT o.order_id)"
_STORES = [STORE_ID, STORE]
# Each case was held before for a grain plan picked; now it is one total over its window, or,
# for a qualification with no window, over all time.
_ONE_TOTAL = [
    # A relative window of several periods.
    (
        "revenue in the last 3 months",
        ["revenue_usd"],
        _TOTAL.format(REVENUE, "2017-05-01", "2017-08-01"),
    ),
    (
        "revenue by store last 7 days",
        [*_STORES, "revenue_usd"],
        _STORE_REVENUE + _WINDOW.format("2017-08-14", "2017-08-21") + " GROUP BY 1, 2",
    ),
    ("orders past 2 weeks", ["order_count"], _TOTAL.format(ORDERS, "2017-08-07", "2017-08-21")),
    (
        "which 3 stores have the highest revenue in the last 6 months",
        [*_STORES, "revenue_usd"],
        _STORE_REVENUE
        + _WINDOW.format("2017-02-01", "2017-08-01")
        + " GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 3",
    ),
    # A dated window of several periods.
    (
        "revenue in 2016 and 2017",
        ["revenue_usd"],
        _TOTAL.format(REVENUE, "2016-01-01", "2018-01-01"),
    ),
    (
        "orders from December 30, 2016 to January 2, 2017",
        ["order_count"],
        _TOTAL.format(ORDERS, "2016-12-30", "2017-01-03"),
    ),
    (
        "revenue by store from January 1 2016 to December 31 2017",
        [*_STORES, "revenue_usd"],
        _STORE_REVENUE + _WINDOW.format("2016-01-01", "2018-01-01") + " GROUP BY 1, 2",
    ),
    # A qualification with no window: each store's or customer's rows over all time.
    (
        "top 3 stores by revenue with at least 4 distinct customers",
        [*_STORES, "revenue_usd"],
        _STORE_REVENUE + " WHERE o.store_id IN (SELECT store_id FROM jaffle_order GROUP BY 1 "
        "HAVING COUNT(DISTINCT customer_id) >= 4) GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 3",
    ),
    (
        "top 10 stores by revenue with at least 10 orders",
        [*_STORES, "revenue_usd"],
        _STORE_REVENUE + " WHERE o.store_id IN (SELECT store_id FROM jaffle_order GROUP BY 1 "
        "HAVING COUNT(DISTINCT order_id) >= 10) GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10",
    ),
    (
        "revenue from customers with at least 10 orders by store",
        [*_STORES, "revenue_usd"],
        _STORE_REVENUE + " WHERE o.customer_id IN (SELECT customer_id FROM jaffle_order GROUP BY 1 "
        "HAVING COUNT(DISTINCT order_id) >= 10) GROUP BY 1, 2",
    ),
]


def _plain(value: Any) -> Any:
    return round(float(value), 2) if isinstance(value, (int, float, Decimal)) else str(value)


@pytest.mark.parametrize(
    ("intent", "columns", "sql"), _ONE_TOTAL, ids=[row[0] for row in _ONE_TOTAL]
)
def test_a_one_total_matches_plain_sql(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str, columns: list[str], sql: str
) -> None:
    # As the generic corpus plans it (test_plan_listed_groupings.py).
    monkeypatch.setattr(jaffle, "_package_examples", [])
    payload = plan_payload(jaffle, intent=intent, now=AUGUST)

    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query"]
    assert "grain" not in query.get("time", {})
    rows = typed_rows(jaffle.query(query, now=AUGUST))
    actual = [tuple(_plain(row[column]) for column in columns) for row in rows]
    expected = [tuple(_plain(value) for value in row) for row in _reference(jaffle, sql)]
    assert expected
    if "limit" not in query:
        actual, expected = sorted(actual), sorted(expected)
    assert actual == expected


def test_a_grain_the_caller_sets_is_asked_for(jaffle: Runtime) -> None:
    caller = {"time": {"temporal_role": ORDER_TIME, "grain": "week"}}
    query = {"group_by": [STORE], "time": {"temporal_role": ORDER_TIME, "grain": "week"}}

    assert (
        unasked_groupings._unasked_grouping_why(
            jaffle, "revenue by store", Call.from_call(query), Call.from_call(caller)
        )
        is None
    )
    why = unasked_groupings._unasked_grouping_why(jaffle, "revenue by store", Call.from_call(query))
    assert why is not None
    assert why["details"] == {"unasked_groupings": ["week"], "grain": "week"}
    # The check never changes the draft.
    assert query == {"group_by": [STORE], "time": {"temporal_role": ORDER_TIME, "grain": "week"}}
