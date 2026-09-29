"""plan resolves time phrases and labels without changing the question.

A time of day used to widen to its whole day, a restated window (``Q1 2017
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
from semantic_rails.planner.faithfulness import unmatched_intent_terms

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


# --- times of day ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("revenue from 12:00 to 13:00 on 15 March 2017", HOUR),
        ("revenue between 12:00 and 13:00 on March 15, 2017", HOUR),
        ("revenue on 2017-03-15 from 12:00 until 13:00", HOUR),
        # A day stated twice, with the range beside it, is still one day.
        ("revenue from 12:00 to 13:00 on 15 March 2017 (2017-03-15)", HOUR),
        (
            "revenue on March 15, 2017 from 9:30 am to 5 pm",
            {"start": f"{MARCH_15}T09:30:00", "end": f"{MARCH_15}T17:00:00"},
        ),
        (
            "orders on March 15, 2017 from 9am to noon",
            {"start": f"{MARCH_15}T09:00:00", "end": f"{MARCH_15}T12:00:00"},
        ),
        (
            "orders on March 15, 2017 from 12:15:30 to 12:45",
            {"start": f"{MARCH_15}T12:15:30", "end": f"{MARCH_15}T12:45:00"},
        ),
    ],
)
def test_a_time_range_on_a_day_resolves_to_timestamps(text: str, bounds: dict[str, str]) -> None:
    assert _time_bounds_from_text(text) == bounds
    assert _unresolved_time_phrases(text) == []
    # A window inside one day is one day bucket, whatever finer grain the role offers.
    assert _time_spec(ORDER_TIME, text)["grain"] == "day"


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        # A lone time is not a range.
        ("revenue at 12:00 on 15 March 2017", "12:00"),
        ("revenue on 15 March 2017 at noon", "noon"),
        ("revenue after 3pm on 15 March 2017", "3pm"),
        # A bound carries no zone, so any zone named is reported, UTC too: the role's zone
        # may be another, and a literal "Z" is dropped when it meets a timestamp without one.
        ("revenue from 12:00 to 13:00 UTC on 15 March 2017", "13:00 utc"),
        ("revenue on March 15, 2017 from 12:00 UTC to 13:00 UTC", "12:00 utc"),
        ("revenue from 12:00 to 13:00 EST on 15 March 2017", "13:00 est"),
        ("revenue from 12:00 to 13:00 +02:00 on 15 March 2017", "+02:00"),
        ("revenue from 12:00 UTC to 13:00 EST on 15 March 2017", "13:00 est"),
        # A zone written any other way is never read as the role's zone.
        ("revenue from 12:00 to 13:00 UTC +2 on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 (UTC+2) on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 (UTC) on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 on 15 March 2017 in UTC", "13:00"),
        ("revenue from 12:00 to 13:00 Pacific time on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 London time on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 AEST on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 HST on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 Europe/Berlin on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00 on 15 March 2017 in EST", "13:00"),
        ("revenue in the Pacific time zone from 12:00 to 13:00 on 15 March 2017", "13:00"),
        # Other words between the range and its day, and a range that ends before it starts.
        ("revenue from 12:00 to 13:00 for stores on 15 March 2017", "13:00"),
        ("revenue from 22:00 to 02:00 on 15 March 2017", "02:00"),
        ("revenue from 13:00 to 13:00 on 15 March 2017", "13:00"),
        ("revenue from 25:00 to 26:00 on 15 March 2017", "25:00"),
        # A range needs one day, and "and" needs "between".
        ("revenue from 12:00 to 13:00 in March 2017", "12:00"),
        ("revenue from 12:00 to 13:00 yesterday", "12:00"),
        ("revenue from 12:00 to 13:00 in 2017", "12:00"),
        ("revenue at 12:00 and 13:00 on 15 March 2017", "13:00"),
        ("revenue from 12:00 to 13:00", "12:00"),
    ],
)
def test_a_time_of_day_it_cannot_resolve_is_reported(text: str, phrase: str) -> None:
    assert _time_bounds_from_text(text) == {}
    phrases = _unresolved_time_phrases(text)
    assert any(phrase in reported for reported in phrases), phrases


def test_a_time_range_states_its_reading() -> None:
    window, zone = _time_window("revenue on 15 March 2017 from 12:00 to 13:00").assumptions
    assert "excludes 13:00" in window and "time.end is exclusive" in window
    assert "No time zone was named" in zone and "temporal role time zone" in zone


def test_plan_keeps_the_hour_it_was_asked_for(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        hour = plan_payload(
            runtime, intent="orders from 12:00 to 13:00 on 15 March 2017", detail="best"
        )
        assert hour["status"] == "ok"
        assert not hour.get("warnings")
        assert {key: _query(hour)["time"][key] for key in ("start", "end")} == HOUR
        assert "time.end is exclusive" in hour["assumptions"][0]
        assert "No time zone was named" in hour["assumptions"][1]
        day = plan_payload(runtime, intent="orders on 15 March 2017", detail="best")
        assert day["status"] == "ok" and "assumptions" not in day
        assert {key: _query(day)["time"][key] for key in ("start", "end")} == {
            "start": MARCH_15,
            "end": "2017-03-16",
        }
        # The window narrows what the engine counts.
        counts = [
            runtime.query(_query(payload))["rows"][0]["order_count"] for payload in (hour, day)
        ]
        assert 0 < counts[0] < counts[1]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "intent",
    [
        "orders at 12:00 on 15 March 2017",
        "orders from 12:00 to 13:00 UTC on 15 March 2017",
        "orders from 12:00 to 13:00 (UTC) on 15 March 2017",
        "orders from 12:00 to 13:00 Pacific time on 15 March 2017",
        "orders from 12:00 to 13:00 EST on 15 March 2017",
        "orders from 12:00 to 13:00 in March 2017",
    ],
)
def test_plan_offers_no_query_for_a_time_it_cannot_resolve(
    runtime_factory: Any, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent, detail="query")
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
        assert any(":" in phrase for phrase in payload["why"]["details"]["unresolved_phrases"])
        assert "query_ir" not in payload["best"]
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


def _draft(measure: str = "measure.jaffle.revenue_usd", **parts: Any) -> dict[str, Any]:
    select = [{"as": "value", "expression": {"measure": measure}}]
    return {"version": 2, "select": select, **parts}


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
