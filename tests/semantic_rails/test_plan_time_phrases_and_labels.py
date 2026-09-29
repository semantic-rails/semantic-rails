"""plan resolves time phrases and labels without changing the question.

A time of day or zone used to widen to its whole day (plan now refuses it), a restated window (``Q1 2017
(January 1 to March 31, 2017)``) was reported as two windows, "item revenue"
resolved to the shorter "revenue", a second measure was dropped, and question
words about how a value is worded ("dated", "placed") drowned out the ones that
mattered. plan now keeps the question's window and subject, or says what it did
not keep.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import (
    _named_measure,
    _time_bounds_from_text,
    _time_spec,
    _time_window,
    _unresolved_time_phrases,
)
from semantic_rails.planner.faithfulness import unconsumed_terms, unmatched_intent_terms

MARCH_15 = "2017-03-15"
HOUR = {"start": f"{MARCH_15}T12:00:00", "end": f"{MARCH_15}T13:00:00"}
Q1_2017 = {"start": "2017-01-01", "end": "2017-04-01"}
YEAR_2017 = {"start": "2017-01-01", "end": "2018-01-01"}
ORDER_TIME = "temporal_role.jaffle_order_time"


def _query(payload: dict[str, Any]) -> dict[str, Any]:
    return (payload.get("best") or {}).get("query_ir") or {}


def _measures(payload: dict[str, Any]) -> list[str]:
    return [
        item["expression"]["measure"]
        for item in _query(payload).get("select", [])
        if "measure" in item["expression"]
    ]


def _draft(measure: str = "measure.jaffle.revenue_usd", **parts: Any) -> dict[str, Any]:
    select = [{"as": "value", "expression": {"measure": measure}}]
    return {"version": 2, "select": select, **parts}


# --- hours, zones and windows shorter than a day --------------------------------------


# The invariant: a draft is ready only if every numeral and every clock or zone word in the
# question is consumed by something the draft carries. Each question below states an hour or a
# zone that no window form resolves; each was once widened to its whole day or dropped while
# the plan reported ok.
HOUR_QUESTIONS = [
    "revenue from 12:00 to 13:00 on 15 March 2017",
    "revenue between 12:00 and 13:00 on March 15, 2017",
    "revenue on 2017-03-15 from 12:00 until 13:00",
    "revenue on March 15, 2017 from 9:30 am to 5 pm",
    "orders on March 15, 2017 from 9am to noon",
    "orders on March 15, 2017 from 12:15:30 to 12:45",
    "revenue at 12:00 on 15 March 2017",
    "revenue on 15 March 2017 at noon",
    "revenue on 15 March 2017 after midnight",
    "revenue after 3pm on 15 March 2017",
    "revenue at 12:00",
    "revenue from 22:00 to 02:00 on 15 March 2017",
    "revenue from 12:00 to 13:00 in March 2017",
    "revenue from 12:00 to 13:00 yesterday",
    "revenue on 15 March 2017 in the morning",
    "revenue on 15 March 2017 at 9 o'clock",
    "orders from 12.30 to 13.30 on 15 March 2017",
    "orders from 9 to 5 on 15 March 2017",
    "orders 1200-1300 hours on 15 March 2017",
    "orders from 1200 hours to 1300 hours on 15 March 2017",
    "orders on 15 March 2017 9 - 17",
    # Hours with no colon or unit-suffix cue beside the day.
    "revenue on 15 March 2017 between 9 and 17",
    "revenue between 12 and 13 on 15 March 2017",
    "revenue at 9 on 15 March 2017",
    "revenue on 15 March 2017 at 14h30",
    "revenue on 15 March 2017 at 14h",
    "revenue on 15 March 2017 at 1400",
    "revenue on 15 March 2017 for 3 hours",
    "orders on 15 March 2017 by the hour",
]
ZONE_QUESTIONS = [
    "orders from 12:00 to 13:00 UTC on 15 March 2017",
    "orders in EST from 12:00 to 13:00 on 15 March 2017",
    "EST: orders from 12:00 to 13:00 on 15 March 2017",
    "orders (PST) from 12:00 to 13:00 on 15 March 2017",
    "orders from 12:00 to 13:00 +02:00 on 15 March 2017",
    "orders from 12:00 to 13:00 (UTC+2) on 15 March 2017",
    "orders on 15 March 2017 from 12:00 to 13:00 Pacific",
    "orders from 12:00 to 13:00 on 15 March 2017 in CET",
    "orders from 12:00 to 13:00 Pacific time on 15 March 2017",
    "orders from 12:00 to 13:00 Europe/Berlin on 15 March 2017",
    "orders from 12:00Z to 13:00Z on 15 March 2017",
    # A zone alone changes the day's edges, with no hour named.
    "orders on 15 March 2017 in UTC",
    "orders on 15 March 2017 in GMT",
    "orders on 15 March 2017 (AEST)",
    "orders on 15 March 2017 in Europe/Berlin",
    "orders on 15 March 2017 in the america/new_york time zone",
]
# Windows shorter than a day: never widened to all time.
SUB_DAY_QUESTIONS = [
    "revenue for the last 24 hours",
    "revenue for the past hour",
    "orders in the last 30 minutes",
    "orders over the previous few hours",
    "revenue this hour",
    "revenue in the trailing 12 hours",
    "revenue by store for the last hour",
]
# Numbers and words that look like an hour or a zone and aren't one. plan reads the window
# they sit beside; a number the draft doesn't carry is named, and never read as an hour.
NOT_AN_HOUR_QUESTIONS = [
    "revenue from customers aged 25-34 in 2017",
    "customers with 2 to 5 orders in 2017",
    "top 10 to 20 products by revenue in Q1 2017",
    "orders with a discount above 10.50 in 2017",
    "orders with 10 to 20 items in March 2017",
    "average delivery time by month",
    "median time between orders in 2017",
    "revenue at the time of order in 2017",
    "revenue by signup time in 2017",
    "revenue by order time in 2017",
    "revenue in 2017 for first time customers",
    "orders in 2017 for the west region",
    "orders by day in Central",
    "orders on 15 March 2017 local time",
]


def _plan(runtime_factory: Any, text: str, **kwargs: Any) -> dict[str, Any]:
    runtime = runtime_factory("jaffle_shop")
    try:
        return plan_payload(runtime, intent=text, detail="best", **kwargs)
    finally:
        runtime.close()


@pytest.mark.parametrize("text", [*HOUR_QUESTIONS, *ZONE_QUESTIONS])
def test_plan_is_never_ready_with_an_hour_or_zone_it_did_not_consume(
    runtime_factory: Any, text: str
) -> None:
    payload = _plan(runtime_factory, text)
    assert payload["status"] == "low_confidence", payload
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] in {"PLAN_UNMATCHED_TERMS", "TIME_WINDOW_UNRESOLVED"}
    if payload["why"]["code"] == "PLAN_UNMATCHED_TERMS":
        assert payload["why"]["details"]["terms"]
        # The terms are named in the message, so a caller sees what was left over.
        assert payload["why"]["details"]["terms"][0] in payload["why"]["message"]


@pytest.mark.parametrize(
    ("text", "terms"),
    [
        ("revenue on 15 March 2017 between 9 and 17", ["9", "17"]),
        ("revenue between 12 and 13 on 15 March 2017", ["12", "13"]),
        ("revenue on 15 March 2017 at 9", ["9"]),
        ("revenue on 15 March 2017 at 14h30", ["14h30"]),
        ("revenue on 15 March 2017 at 1400", ["1400"]),
        ("revenue on 15 March 2017 at noon", ["noon"]),
        ("orders on 15 March 2017 by the hour", ["hour"]),
        ("orders on 15 March 2017 in UTC", ["utc"]),
        ("orders on 15 March 2017 (AEST)", ["aest"]),
        ("orders on 15 March 2017 in Europe/Berlin", ["europe/berlin"]),
        ("orders in the tz of the store", ["tz"]),
        ("revenue for stores over 12.50 in 2017", ["12.50"]),
    ],
)
def test_every_unconsumed_number_and_clock_word_is_named(
    runtime_factory: Any, text: str, terms: list[str]
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        assert unconsumed_terms(runtime, text, _draft()) == terms
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("top 10 stores by revenue", _draft(limit=10)),
        ("stores with revenue over 12.50", _draft(having=[{"op": ">", "value": 12.5}])),
        ("stores with 1,000 or more orders", _draft(having=[{"op": ">=", "value": 1000}])),
        ("stores in the 90th percentile", _draft(having=[{"op": ">", "value": 90}])),
        ("revenue on 15 March 2017", _draft()),
        ("revenue in Q1 2017 by month", _draft()),
        ("revenue for the last 7 days", _draft()),
        ("average delivery time by month", _draft()),
        ("revenue by order time in 2017", _draft()),
        # A caller's stated hours account for the question's.
        ("revenue from 12:00 to 13:00 on 15 March 2017", _draft(time={"grain": "day", **HOUR})),
    ],
)
def test_a_number_or_clock_word_the_draft_carries_is_consumed(
    runtime_factory: Any, text: str, query: dict[str, Any]
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        assert unconsumed_terms(runtime, text, query) == []
    finally:
        runtime.close()


@pytest.mark.parametrize("text", SUB_DAY_QUESTIONS)
def test_a_window_shorter_than_a_day_is_unresolved_and_never_all_time(
    runtime_factory: Any, text: str
) -> None:
    window = _time_window(text)
    assert window.bounds == {} and window.sub_day != () and window.unresolved != ()
    assert _time_bounds_from_text(text) == {}
    assert not {"start", "end", "range"} & set(_time_spec(ORDER_TIME, text))
    payload = _plan(runtime_factory, text)
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert payload["why"]["details"]["sub_day_phrases"]
    assert payload["why"]["recovery_hints"][0]["kind"] == "state_hour_range"
    assert "query_ir" not in payload["best"] and "validate" not in payload["next"]


def test_plan_accepts_a_window_shorter_than_a_day_the_caller_states(runtime_factory: Any) -> None:
    payload = _plan(
        runtime_factory,
        "revenue for the last 24 hours",
        partial_query={"time": {"temporal_role": ORDER_TIME, "grain": "day", **HOUR}},
    )
    assert payload["status"] == "ok", payload.get("why")
    assert {key: _query(payload)["time"][key] for key in ("start", "end")} == HOUR


@pytest.mark.parametrize("text", NOT_AN_HOUR_QUESTIONS)
def test_a_number_or_time_word_that_is_not_an_hour_is_never_refused_as_one(
    runtime_factory: Any, text: str
) -> None:
    window = _time_window(text)
    assert window.sub_day == () and window.unresolved == ()
    payload = _plan(runtime_factory, text)
    why = payload.get("why") or {}
    assert why.get("code") != "TIME_WINDOW_UNRESOLVED", why
    if why.get("code") == "PLAN_UNMATCHED_TERMS":
        # Left over, and named, only because the draft doesn't carry the number.
        assert all(term.replace(".", "").isdigit() for term in why["details"]["terms"]), why


@pytest.mark.parametrize(
    "text",
    [
        "average delivery time by month in 2017",
        "revenue by order time in 2017",
        "top 5 stores by revenue in 2017",
        "revenue for the last 3 months by store",
        "orders in Q1 2017 by month",
    ],
)
def test_a_question_whose_numbers_and_time_words_are_all_consumed_is_ok(
    runtime_factory: Any, text: str
) -> None:
    payload = _plan(runtime_factory, text)
    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]


@pytest.mark.parametrize("detail", ["best", "query", "full", "debug"])
@pytest.mark.parametrize("partial", [None, {"limit": 5}, {"time": {"grain": "day"}}])
def test_no_detail_level_or_caller_block_gets_past_the_invariant(
    runtime_factory: Any, detail: str, partial: dict[str, Any] | None
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime,
            intent="revenue on 15 March 2017 between 9 and 17",
            partial_query=partial,
            detail=detail,
        )
        assert payload["status"] == "low_confidence"
        assert "ready_for" not in payload.get("next", {})
        assert payload["why"]["details"]["terms"] == ["9", "17"]
    finally:
        runtime.close()


def test_a_day_is_still_resolved_as_a_day() -> None:
    window = _time_window("revenue on 15 March 2017")
    assert window.bounds == {"start": MARCH_15, "end": "2017-03-16"}
    assert window.unresolved == () and window.assumptions == ()


def test_plan_refuses_an_hour_on_a_date_role_too(runtime_factory: Any) -> None:
    # The invariant doesn't depend on the role's column type, so a date column never gets a
    # timestamp bound it can't compare.
    runtime = runtime_factory("tpch_sf1_showcase")
    try:
        payload = plan_payload(
            runtime, intent="orders from 12:00 to 13:00 on 15 March 1995", detail="best"
        )
        assert payload["status"] == "low_confidence"
        assert "ready_for" not in payload["next"]
        assert payload["why"]["code"] in {"PLAN_UNMATCHED_TERMS", "TIME_WINDOW_UNRESOLVED"}
    finally:
        runtime.close()


def test_plan_accepts_an_hour_range_the_caller_states(runtime_factory: Any) -> None:
    payload = _plan(
        runtime_factory,
        "orders from 12:00 to 13:00 on 15 March 2017",
        partial_query={"time": {"temporal_role": ORDER_TIME, "grain": "day", **HOUR}},
    )
    assert payload["status"] == "ok", payload.get("why")
    assert {key: _query(payload)["time"][key] for key in ("start", "end")} == HOUR


def test_plan_still_plans_a_day(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        day = plan_payload(runtime, intent="orders on 15 March 2017", detail="best")
        assert day["status"] == "ok" and "assumptions" not in day
        assert {key: _query(day)["time"][key] for key in ("start", "end")} == {
            "start": MARCH_15,
            "end": "2017-03-16",
        }
    finally:
        runtime.close()


# --- restated windows -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("revenue in Q1 2017 (January 1 to March 31, 2017)", Q1_2017),
        ("revenue for January 1 to March 31, 2017 (Q1 2017)", Q1_2017),
        ("revenue in Q1 2017 (2017-01-01 to 2017-03-31)", Q1_2017),
        ("revenue in the first quarter of 2017 (Q1 2017)", Q1_2017),
        ("revenue in 2017 (January 2017 through December 2017)", YEAR_2017),
        ("revenue in calendar year 2017 (2017-01-01 to 2017-12-31)", YEAR_2017),
        (
            "revenue in the first half of 2017 (H1 2017)",
            {"start": "2017-01-01", "end": "2017-07-01"},
        ),
    ],
)
def test_a_window_stated_twice_the_same_way_is_one_window(
    text: str, bounds: dict[str, str]
) -> None:
    window = _time_window(text)
    assert window.bounds == bounds
    assert window.unresolved == () and window.conflicts == ()


@pytest.mark.parametrize(
    "text",
    [
        # Equal bounds, but a second condition: not a restatement.
        "revenue in 2017 from customers who signed up in 2017",
        "orders in Q1 2017 by stores opened in Q1 2017",
        "revenue in 2017 for products launched in 2017",
        "revenue in Q1 2017 and orders in Q1 2017",
        "revenue in 2017 (excluding stores opened in 2017)",
    ],
)
def test_the_same_window_beside_another_condition_is_not_a_restatement(text: str) -> None:
    window = _time_window(text)
    assert window.bounds == {}
    assert len(window.conflicts) >= 1 and window.unresolved != ()


@pytest.mark.parametrize(
    ("text", "phrases"),
    [
        (
            "revenue in Q1 2017 (January 1 to March 30, 2017)",
            ("q1 2017", "january 1 to march 30, 2017"),
        ),
        ("revenue in Q1 2017 (April 1 to June 30, 2017)", ("q1 2017", "april 1 to june 30, 2017")),
        ("revenue in 2017 (January 2017 through November 2017)", ("2017", "january 2017")),
        ("revenue in H1 2017 (Q1 2017)", ("h1 2017", "q1 2017")),
    ],
)
def test_windows_that_differ_are_a_named_conflict(text: str, phrases: tuple[str, ...]) -> None:
    window = _time_window(text)
    assert window.bounds == {}
    assert len(window.conflicts) == 2
    for phrase in phrases:
        assert any(phrase in reported for reported in window.conflicts), window.conflicts
        assert any(phrase in reported for reported in window.unresolved), window.unresolved


def test_plan_names_the_two_windows_that_differ(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        ok = plan_payload(
            runtime, intent="revenue in Q1 2017 (January 1 to March 31, 2017)", detail="query"
        )
        assert ok["status"] == "ok"
        assert {key: _query(ok)["time"][key] for key in ("grain", "start", "end")} == {
            "grain": "quarter",
            **Q1_2017,
        }
        assert "time.end is 2017-04-01" in ok["assumptions"][0]
        clash = plan_payload(
            runtime, intent="revenue in Q1 2017 (January 1 to March 30, 2017)", detail="query"
        )
        assert clash["status"] == "low_confidence"
        assert clash["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
        assert "differ" in clash["why"]["message"]
        assert len(clash["why"]["details"]["conflicting_phrases"]) == 2
        assert "query_ir" not in clash["best"]
    finally:
        runtime.close()


# --- window forms -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("revenue in the first half of 2017", {"start": "2017-01-01", "end": "2017-07-01"}),
        ("revenue in the second half of 2017", {"start": "2017-07-01", "end": "2018-01-01"}),
        ("revenue for first half 2017", {"start": "2017-01-01", "end": "2017-07-01"}),
        ("revenue from March 1 to March 31, 2017", {"start": "2017-03-01", "end": "2017-04-01"}),
        ("revenue from March 1 to 31, 2017", {"start": "2017-03-01", "end": "2017-04-01"}),
        ("revenue Mar 1 - Mar 31 2017", {"start": "2017-03-01", "end": "2017-04-01"}),
        ("revenue Mar 1 - 31 2017", {"start": "2017-03-01", "end": "2017-04-01"}),
        ("revenue between 2017-03-01 and 2017-03-31", {"start": "2017-03-01", "end": "2017-04-01"}),
        ("revenue 2017-03-01 to 2017-03-31", {"start": "2017-03-01", "end": "2017-04-01"}),
        (
            "revenue from 2017-03-01 through 2017-03-31",
            {"start": "2017-03-01", "end": "2017-04-01"},
        ),
        ("revenue in calendar year 2017", YEAR_2017),
        ("revenue for the calendar year 2017", YEAR_2017),
        ("revenue in year 2017", YEAR_2017),
        ("revenue for year 2017", YEAR_2017),
        ("year 2017 revenue", YEAR_2017),
        ("calendar year 2017 revenue by store", YEAR_2017),
    ],
)
def test_more_window_forms_resolve(text: str, bounds: dict[str, str]) -> None:
    assert _time_bounds_from_text(text) == bounds
    assert _unresolved_time_phrases(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "revenue vs prior year 2016",
        "revenue compared with the previous year 2016",
        "revenue per year 2017",
        "revenue since year 2017",
        "revenue before the year 2017",
        "revenue at the end of year 2017",
        "revenue in fiscal year 2017",
        "revenue for last year 2017",
        # A different kind of year is not the calendar year.
        "revenue for the financial year 2017",
        "revenue in tax year 2017",
        "units sold for model year 2017",
        "enrolments for school year 2017",
        "revenue for academic year 2017",
        "units sold in model-year 2017",
    ],
)
def test_a_year_after_the_word_year_is_not_always_a_window(text: str) -> None:
    assert _time_bounds_from_text(text) == {}
    assert _unresolved_time_phrases(text) != []


@pytest.mark.parametrize(
    ("text", "sentence"),
    [
        ("revenue 2017-03-01 to 2017-03-31", "includes its last day, so time.end is 2017-04-01"),
        (
            "revenue from March 1 to March 31, 2017",
            "includes its last day, so time.end is 2017-04-01",
        ),
        (
            "revenue from January 2017 through March 2017",
            "includes its last month, so time.end is 2017-04-01",
        ),
        ("revenue between 2016 and 2017", "includes its last year, so time.end is 2018-01-01"),
    ],
)
def test_a_range_states_that_its_last_day_is_included(text: str, sentence: str) -> None:
    (assumption,) = _time_window(text).assumptions
    assert sentence in assumption


@pytest.mark.parametrize(
    "text",
    ["revenue in March 2017", "revenue in Q1 2017", "revenue in 2017", "revenue last month"],
)
def test_a_window_with_no_open_end_states_no_assumption(text: str) -> None:
    assert _time_window(text).assumptions == ()


# --- labels -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "measure"),
    [
        ("item revenue in 2017", "measure.jaffle.item_revenue_usd"),
        ("total item revenue by month", "measure.jaffle.item_revenue_usd"),
        ("Item revenue (USD) in 2017", "measure.jaffle.item_revenue_usd"),
        ("revenue in 2017", "measure.jaffle.revenue_usd"),
        # The words are not a name when they are not adjacent, or name another thing.
        ("revenue by item in 2017", "measure.jaffle.revenue_usd"),
        ("food revenue in 2017", "measure.jaffle.food_revenue_usd"),
        # A name inside other words is not the ask: this is revenue, not the count of large orders.
        ("large order revenue by month", "measure.jaffle.revenue_usd"),
        ("large orders by month", "measure.jaffle.large_order_count"),
    ],
)
def test_an_exact_multi_word_label_outranks_a_partial_one(
    runtime_factory: Any, text: str, measure: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=text, detail="query")
        assert _measures(payload) == [measure]
        assert payload["status"] == "ok", payload.get("why")
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("text", "replaces_target"),
    [
        ("item revenue by month", True),
        # A ratio or growth question keeps its metric-first target: a measure's full name
        # never replaces it.
        ("item revenue growth by month", False),
        ("item revenue share by product", False),
        ("item revenue per order by store", False),
    ],
)
def test_a_full_measure_name_never_replaces_a_ratio_or_growth_target(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch, text: str, replaces_target: bool
) -> None:
    from semantic_rails.planner._base import _tokens
    from semantic_rails.planner.patterns import metric_by_dimension_rollup as rollup

    asked: list[str] = []
    real = rollup._named_measure
    monkeypatch.setattr(
        rollup,
        "_named_measure",
        lambda *args, **kwargs: asked.append(text) or real(*args, **kwargs),
    )
    runtime = runtime_factory("jaffle_shop")
    try:
        assert rollup._match(runtime, text, set(_tokens(text))) is not None
        assert bool(asked) is replaces_target
    finally:
        runtime.close()


def test_a_name_is_the_longest_exact_one(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        config = runtime._config
        assert (
            _named_measure(config, "item revenue by month").id == "measure.jaffle.item_revenue_usd"
        )
        assert (
            _named_measure(config, "gross profit in 2017").id == "measure.jaffle.gross_profit_usd"
        )
        # A name replaces the ordinary target only when it holds every word of that target.
        revenue = next(row for row in config.measures if row.id == "measure.jaffle.revenue_usd")
        large = next(row for row in config.measures if row.id == "measure.jaffle.large_order_count")
        assert (
            _named_measure(config, "item revenue by month", revenue).id
            == "measure.jaffle.item_revenue_usd"
        )
        assert _named_measure(config, "large order revenue by month", revenue) is None
        assert _named_measure(config, "large orders by month", large).id == large.id
        assert _named_measure(config, "large orders by month", revenue) is None
        # Another measure noun right after the name leaves the question to the ordinary ranking.
        assert _named_measure(config, "item revenue orders") is None
        assert _named_measure(config, "item revenue by orders") is not None
        # One word is no multi-word name, and words out of order are none either.
        assert _named_measure(config, "revenue by month") is None
        assert _named_measure(config, "revenue item") is None
        assert _named_measure(config, "") is None
    finally:
        runtime.close()


# --- every requested measure --------------------------------------------------------


@pytest.mark.parametrize(
    ("intent", "missing"),
    [
        ("item revenue and orders in Q1 2017", "orders"),
        ("revenue and item revenue in 2017", "revenue"),
        ("gross profit and revenue for the first half of 2017", ("gross profit", "revenue")),
        ("revenue and orders by store", "orders"),
        ("orders and revenue in March 2017", ("orders", "revenue")),
        (
            "revenue, orders and gross profit from March 1 to March 31, 2017",
            ("orders", "gross profit"),
        ),
        ("item revenue along with orders in 2017", "orders"),
    ],
)
def test_a_second_measure_is_never_dropped_silently(
    runtime_factory: Any, intent: str, missing: str | tuple[str, ...]
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent, detail="query")
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        gaps = [gap for gap in payload["why"]["details"]["gaps"] if "subjects" in gap["kind"]]
        assert gaps, payload["why"]
        named = {row["phrase"] for gap in gaps for row in gap["actual"]["missing"]}
        assert named & set((missing,) if isinstance(missing, str) else missing), gaps
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "intent",
    ["revenue in Q1 2017", "revenue by store for the first half of 2017"],
)
def test_a_single_measure_still_plans(runtime_factory: Any, intent: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent, detail="query")
        assert payload["status"] == "ok", payload.get("why")
        assert _measures(payload) == ["measure.jaffle.revenue_usd"]
        assert "why" not in payload
    finally:
        runtime.close()


# --- unmatched terms ---------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "revenue dated in 2017",
        "revenue placed only in 2017",
        "revenue counted using the order time",
        "revenue that came in during 2017 while using such",
        "revenue anchored on the order time",
        "revenue placed only in 2017 while using order time counted such as dated",
    ],
)
def test_function_words_and_verbs_are_not_unmatched(runtime_factory: Any, question: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        assert unmatched_intent_terms(runtime, question, _draft()) == []
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("question", "query", "expected"),
    [
        # A number the draft doesn't carry, and the comparative beside it.
        ("orders placed 2 or more in 2017", _draft("measure.jaffle.order_count"), ["2", "more"]),
        (
            "orders over 500 dollars in 2017",
            _draft("measure.jaffle.order_count"),
            ["500", "dollars"],
        ),
        ("revenue for the 3 best months", _draft(), ["3"]),
        # A measure noun the draft doesn't use.
        ("revenue dated in 2017 and profitability", _draft(), ["profitability"]),
        ("orders dated in 2017 and refunds", _draft("measure.jaffle.order_count"), ["refunds"]),
    ],
)
def test_numbers_comparatives_and_measure_nouns_stay_unmatched(
    runtime_factory: Any, question: str, query: dict[str, Any], expected: list[str]
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        assert unmatched_intent_terms(runtime, question, query) == expected
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("question", "query"),
    [
        # A number the draft carries: a limit, a threshold, a percentage.
        ("top 5 stores by revenue", _draft(limit=5)),
        ("stores with revenue over 500 dollars", _draft(having=[{"op": ">", "value": 500}])),
        ("stores with more than 50 percent of revenue", _draft(having=[{"op": ">", "value": 0.5}])),
        ("orders in the 7 days", _draft()),
        # A year is the draft's window, whoever set it.
        ("revenue for 2017 please", _draft(time={"grain": "year", **YEAR_2017})),
        ("revenue last 30 days", _draft()),
    ],
)
def test_numbers_the_draft_carries_are_matched(
    runtime_factory: Any, question: str, query: dict[str, Any]
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        assert not {"5", "500", "50", "2017", "30"} & set(
            unmatched_intent_terms(runtime, question, query)
        )
    finally:
        runtime.close()


# --- names the draft dropped -----------------------------------------------------------


@pytest.mark.parametrize(
    "intent",
    [
        "item revenue by product for tangaroo and vanilla ice in 2017",
        "combined item revenue from doctor stew, the krautback and mel-bun by quarter",
    ],
)
def test_a_list_of_names_the_draft_does_not_filter_on_is_not_ok(
    runtime_factory: Any, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent, detail="query")
        assert _measures(payload) == ["measure.jaffle.item_revenue_usd"]
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert len(payload["why"]["details"]["terms"]) >= 2
        assert payload["warnings"][0]["details"]["terms"] == payload["why"]["details"]["terms"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "intent",
    ["revenue with YoY", "revenue from top decile of customers by lifetime spend"],
)
def test_a_single_word_the_planner_reads_elsewhere_is_only_a_warning(
    runtime_factory: Any, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent, detail="query")
        assert payload["status"] == "ok", payload.get("why")
        assert "why" not in payload
        assert _measures(payload)[0] == "measure.jaffle.revenue_usd"
        (warning,) = payload["warnings"]
        assert warning["code"] == "PLAN_UNMATCHED_TERMS"
    finally:
        runtime.close()
