"""plan resolves time phrases and labels without changing the question.

A time of day or zone used to widen to its whole day (plan now refuses it), a restated window (``Q1 2017
(January 1 to March 31, 2017)``) was reported as two windows, "item revenue"
resolved to the shorter "revenue", a second measure was dropped, and question
words about how a value is worded ("dated", "placed") drowned out the ones that
mattered. plan now keeps the question's window and subject, or says what it did
not keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import _tokens
from semantic_rails.planner.call import Call
from semantic_rails.planner.consumed_spans import unconsumed_terms as _unconsumed_terms
from semantic_rails.planner.groupings import _time_spec
from semantic_rails.planner.patterns import metric_by_dimension_rollup as rollup
from semantic_rails.planner.patterns.metric_by_dimension_rollup import (
    _named_measure,
    _unresolved_time_phrases,
)
from semantic_rails.planner.time_checks import _caller_window_gaps
from semantic_rails.planner.time_windows import _time_bounds_from_text, _time_window
from semantic_rails.planner.unmatched_words import unmatched_intent_terms
from semantic_rails.runtime import Runtime
from tests.semantic_rails.planner_fixtures import jaffle_runtime
from tests.semantic_rails.result_helpers import disable_planner_patterns

MARCH_15 = "2017-03-15"
HOUR = {"start": f"{MARCH_15}T12:00:00", "end": f"{MARCH_15}T13:00:00"}
Q1_2017 = {"start": "2017-01-01", "end": "2017-04-01"}
YEAR_2017 = {"start": "2017-01-01", "end": "2018-01-01"}
ORDER_TIME = "temporal_role.jaffle_order_time"
# A draft's window, as the planner sets it for a question that names a date or a range: below,
# `unconsumed_terms` reads it as the window the question states. Its presence is what counts
# here; the question's own spans are what it consumes.
WINDOW = {"grain": "day", **YEAR_2017}
LAST_7_DAYS = {"grain": "day", "last": {"unit": "day", "value": 7}}
DAY_BOUNDS = {"start": f"{MARCH_15}T00:00:00", "end": "2017-03-16T00:00:00"}
DATE_BOUNDS = {"start": MARCH_15, "end": "2017-03-16"}
NOON_TO_MIDNIGHT = {"start": f"{MARCH_15}T12:00:00", "end": "2017-03-16T00:00:00"}
BUSINESS_HOURS = {"start": f"{MARCH_15}T09:30:00", "end": f"{MARCH_15}T17:00:00"}
MARCH_2017 = {"start": "2017-03-01", "end": "2017-04-01"}
SOMEONE_ELSES_DAY = {"start": "2018-06-01", "end": "2018-06-02"}


runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def unconsumed_terms(runtime: Any, text: str, query: dict[str, Any]) -> list[str]:
    bounds = _time_window(text.lower()).bounds if query.get("time") is WINDOW else {}
    if bounds:
        query = {**query, "time": {"grain": "day", **bounds}}
    return _unconsumed_terms(runtime, text, Call.from_call(query))


def _query(payload: dict[str, Any]) -> dict[str, Any]:
    return (payload.get("best") or {}).get("query") or {}


def _measures(payload: dict[str, Any]) -> list[str]:
    return [item.ref for item in Call.from_call(_query(payload)).select if item.kind == "measure"]


def _draft(measure: str = "measure.jaffle.revenue_usd", **parts: Any) -> dict[str, Any]:
    return {"select": [{"expression": measure, "as": "value"}], **parts}


def _caller(time: dict[str, Any]) -> dict[str, Any]:
    """A draft over the caller's own window on the order time."""
    return _draft(time={"temporal_role": ORDER_TIME, "grain": "day", **time})


def _over(value: float, op: str = "gt") -> list[dict[str, Any]]:
    return [{"field": "value", "op": op, "value": value}]


def _plan(runtime: Runtime, text: str, **kwargs: Any) -> dict[str, Any]:
    return plan_payload(runtime, intent=text, detail="best", **kwargs)


def _unmatched_subject(payload: dict[str, Any], terms: list[str]) -> None:
    """No measure is named by the question's words: no draft, never ready."""
    assert payload["status"] == "unrealizable", payload
    assert payload["best"] is None
    assert payload["why"]["code"] == "PLAN_SUBJECT_UNMATCHED"
    assert payload["why"]["details"]["terms"] == terms


def _held(payload: dict[str, Any], *codes: str) -> None:
    assert payload["status"] == "low_confidence", payload
    assert "ready_for" not in payload.get("next", {})
    assert not codes or payload["why"]["code"] in codes, payload["why"]


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
# Consumption is by span: a number, number word or time-unit token is consumed only where a
# construct the draft carries reads it, never because its value equals something in the draft.
# Each question below was once `ok`.
SPAN_QUESTIONS = [
    # A 24-hour time from 1900 to 2099 is not a year.
    "revenue on 15 March 2017 at 2000",
    "revenue on 15 March 2017 at 1930",
    "revenue on 15 March 2017 from 1900 to 2000 hours",
    "revenue on 15 March 2017 from 1900 to 2100",
    # A threshold that looks like a year, when the draft doesn't carry it.
    "customers with 1999 or more orders in 2017",
    # Spelled-out hours and "o'clock".
    "revenue on 15 March 2017 from nine to five",
    "revenue on 15 March 2017 at nine o'clock",
    "revenue on 15 March 2017 between two and four",
    "revenue on 15 March 2017 at twelve",
    "revenue on 15 March 2017 at eleven",
    "revenue on 15 March 2017 at half past two",
    "revenue on 15 March 2017 at quarter to five",
    "revenue on 15 March 2017 at twenty past three",
    "revenue on 15 March 2017 at o'clock",
    # Short zone codes and zones written in another case.
    "orders on 15 March 2017 in ET",
    "orders on 15 March 2017 in PT",
    "orders on 15 March 2017 in CT",
    "orders on 15 March 2017 in MT",
    "orders on 15 March 2017 in est",
    "orders on 15 March 2017 in pst",
    "ORDERS ON 15 MARCH 2017 IN EST",
    "orders on 15 March 2017 in MSK",
    "orders on 15 March 2017 in WIB",
    "orders on 15 March 2017 in AEDT",
]
# A token that is not one number: a dotted date, a version, an address.
NOT_ONE_NUMBER_QUESTIONS = [
    "revenue on 15.03.2017",
    "revenue in 2017 for version 1.2.3",
    "1.234.567 orders",
    "revenue for 10.0.0.1",
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
# A number range the draft does not carry is named.
NUMBER_RANGES = {
    "revenue from customers aged 25-34 in 2017",
    "customers with 2 to 5 orders in 2017",
    "orders with 10 to 20 items in March 2017",
}


@pytest.mark.parametrize(
    "text", [*HOUR_QUESTIONS, *ZONE_QUESTIONS, *SPAN_QUESTIONS, *NOT_ONE_NUMBER_QUESTIONS]
)
def test_plan_is_never_ready_with_a_number_hour_or_zone_it_did_not_consume(
    runtime: Runtime, text: str
) -> None:
    payload = _plan(runtime, text)
    _held(payload, "PLAN_UNMATCHED_TERMS", "TIME_WINDOW_UNRESOLVED")
    if payload["why"]["code"] == "PLAN_UNMATCHED_TERMS":
        assert payload["why"]["details"]["terms"]
        # The terms are named in the message, so a caller sees what was left over.
        assert payload["why"]["details"]["terms"][0] in payload["why"]["message"]


@pytest.mark.parametrize("text", SUB_DAY_QUESTIONS)
def test_a_window_shorter_than_a_day_is_unresolved_and_never_all_time(
    runtime: Runtime, text: str
) -> None:
    window = _time_window(text)
    assert window.bounds == {} and window.sub_day != () and window.unresolved != ()
    assert _time_bounds_from_text(text) == {}
    spec = _time_spec(ORDER_TIME, text)
    assert spec is None or not spec.window
    payload = _plan(runtime, text)
    _held(payload, "TIME_WINDOW_UNRESOLVED")
    assert payload["why"]["details"]["sub_day_phrases"]
    assert payload["why"]["recovery_hints"][0]["kind"] == "state_hour_range"
    assert "query" not in payload["best"] and "validate" not in payload["next"]


@pytest.mark.parametrize("text", NOT_AN_HOUR_QUESTIONS)
def test_a_number_or_time_word_that_is_not_an_hour_is_never_read_as_one(
    runtime: Runtime, text: str
) -> None:
    window = _time_window(text)
    assert window.sub_day == () and window.unresolved == ()
    payload = _plan(runtime, text)
    why = payload.get("why") or {}
    assert why.get("code") != "TIME_WINDOW_UNRESOLVED", why
    if text == "average delivery time by month":
        # No measure is named by "delivery": plan offers no draft on average order value.
        _unmatched_subject(payload, ["delivery"])
        return
    unknown = {
        # The intent parse records "region" as "geo", which nothing resolves.
        "orders in 2017 for the west region": ["west", "region"],
        "orders by day in Central": ["central"],
        "orders on 15 March 2017 local time": ["local"],
    }
    if text in unknown:
        _held(payload)
        assert why["details"] == {"terms": unknown[text], "kind": "filter_values_unrealized"}
    if text in NUMBER_RANGES:
        _held(payload, "PLAN_UNMATCHED_TERMS")
        assert all(term.replace(".", "").isdigit() for term in why["details"]["terms"])
    if why.get("code") == "PLAN_UNMATCHED_TERMS":
        terms = why["details"]["terms"]
        if why["details"].get("kind") == "filter_values_unrealized":
            assert not set(terms) & {"hour", "hours", "clock"}, why
        elif why["recovery_hints"][0]["kind"] == "use_named_objects":
            # A word naming a catalog object the draft doesn't use ("median", "customers").
            assert "time" not in terms, why
        else:
            # Left over, and named, only because the draft doesn't carry the number.
            assert all(term.replace(".", "").isdigit() for term in terms), why


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        ("average delivery time by month in 2017", None),
        ("revenue by order time in 2017", None),
        # Without "monthly", the draft's split into months is a grouping the question never asks
        # for.
        ("monthly revenue for the last 3 months by store", None),
        ("orders in Q1 2017 by month", None),
        ("revenue on 15 March 2017", None),
        ("revenue in March 2017", None),
        ("revenue in 2017", None),
        ("revenue in March 2017 by day", None),
        ("revenue for Q1 2017", None),
        # Without "daily", the draft's split into days is a grouping the question never asks for.
        ("daily revenue for the last 30 days", None),
        ("orders on 15 March 2017 by store", None),
        ("revenue by month in 2017", None),
        ("top 5 stores by revenue", 5),
        ("top five stores by revenue", 5),
        ("the top 5 stores by revenue", 5),
        ("top 3 stores by orders", 3),
        ("top 5 stores by revenue in 2017", 5),
    ],
)
def test_a_question_whose_numbers_and_time_words_are_all_consumed_is_ok(
    runtime: Runtime, text: str, limit: int | None
) -> None:
    payload = _plan(runtime, text)
    if "delivery" in text:
        _unmatched_subject(payload, ["delivery"])
        return
    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]
    assert _query(payload).get("limit") == limit


def test_plan_still_plans_a_day(runtime: Runtime) -> None:
    day = _plan(runtime, "orders on 15 March 2017")
    assert day["status"] == "ok" and "assumptions" not in day
    assert {key: _query(day)["time"][key] for key in ("start", "end")} == DATE_BOUNDS


@pytest.mark.parametrize(
    ("detail", "partial"),
    [
        ("best", None),
        ("query", {"limit": 5}),
        ("full", {"time": {"grain": "day"}}),
        ("debug", None),
    ],
)
def test_no_detail_level_or_caller_block_gets_past_the_invariant(
    runtime: Runtime, detail: str, partial: dict[str, Any] | None
) -> None:
    payload = plan_payload(
        runtime,
        intent="revenue on 15 March 2017 between 9 and 17",
        partial_query=partial,
        detail=detail,
    )
    _held(payload)
    assert payload["why"]["details"]["terms"] == ["9", "17"]


W = _draft(time=WINDOW)
CONSUMED = [
    # Every number and clock or zone word no construct reads is named.
    ("revenue on 15 March 2017 between 9 and 17", W, ["9", "17"]),
    ("revenue between 12 and 13 on 15 March 2017", W, ["12", "13"]),
    ("revenue on 15 March 2017 at 9", W, ["9"]),
    ("revenue on 15 March 2017 at 14h30", W, ["14h30"]),
    ("revenue on 15 March 2017 at 1400", W, ["1400"]),
    ("revenue on 15 March 2017 at noon", W, ["noon"]),
    ("orders on 15 March 2017 by the hour", W, ["hour"]),
    ("orders on 15 March 2017 in UTC", W, ["utc"]),
    ("orders on 15 March 2017 (AEST)", W, ["aest"]),
    ("orders on 15 March 2017 in Europe/Berlin", W, ["europe/berlin"]),
    ("orders in the tz of the store", W, ["tz"]),
    ("revenue for stores over 12.50 in 2017", W, ["12.50"]),
    # A number or clock word the draft carries is consumed.
    ("top 10 stores by revenue", _draft(limit=10), []),
    ("stores with revenue over 12.50", _draft(having=_over(12.5)), []),
    ("stores with 1,000 or more orders", _draft(having=_over(1000, "gte")), []),
    ("stores in the 90th percentile", _draft(having=_over(90)), []),
    ("revenue on 15 March 2017", W, []),
    ("revenue in Q1 2017 by month", W, []),
    ("revenue for the last 7 days", _draft(time=LAST_7_DAYS), []),
    ("average delivery time by month", _draft(), []),
    ("revenue by order time in 2017", W, []),
    # Ordinary analytics words are not clock or zone words.
    ("min order value by store", W, []),
    ("max and min revenue per store", W, []),
    ("NET revenue in 2017", W, []),
    ("EBIT margin in 2017", W, []),
    ("revenue in the WEST region", W, []),
    ("revenue by FIRST order", W, []),
    ("COUNT of orders EVENT", W, []),
    ("orders in the cat category", W, []),
    ("revenue by day", W, []),
    # A year is consumed only by a window's own span.
    ("revenue on 15 March 2017 at 1930", W, ["1930"]),
    ("revenue at 2000", _draft(), ["2000"]),
    ("revenue in 2017 at 2000", _draft(), ["2017", "2000"]),
    # A threshold that looks like a year is consumed only by its own threshold span.
    ("stores with more than 2000 orders in 2017", W, ["2000"]),
    ("stores with more than 2000 orders in 2017", _draft(time=WINDOW, having=_over(2000)), []),
    (
        "customers with 1999 or more orders in 2017",
        _draft(time=WINDOW, having=_over(1999, "gte")),
        [],
    ),
    # The same number elsewhere is not consumed by it.
    ("revenue at 9 for the top 9 stores", _draft(limit=9), ["9"]),
    ("revenue on 15 March 2017 at 100", _draft(time=WINDOW, limit=100), ["100"]),
    ("top 100 stores by revenue in 2017", _draft(time=WINDOW, limit=100), []),
    # Number words: a limit reads its own; nothing else does.
    ("top five stores by revenue in 2017", _draft(time=WINDOW, limit=5), []),
    ("top ten stores by revenue in 2017", _draft(time=WINDOW, limit=5), ["ten"]),
    ("revenue at five on 15 March 2017", _draft(time=WINDOW, limit=5), ["five"]),
    ("revenue on 15 March 2017 from nine to five", W, ["nine", "five"]),
    ("revenue on 15 March 2017 at nine o'clock", W, ["nine", "clock"]),
    ("revenue on 15 March 2017 at half past two", W, ["half", "two"]),
    ("revenue on 15 March 2017 at quarter to five", W, ["quarter", "five"]),
    ("revenue on 15 March 2017 at twelve", W, ["twelve"]),
    ("revenue in the last quarter of 2017", W, []),
    # Zones.
    ("orders on 15 March 2017 in ET", W, ["et"]),
    ("orders on 15 March 2017 in est", W, ["est"]),
    ("ORDERS ON 15 MARCH 2017 IN EST", W, ["est"]),
    ("orders on 15 March 2017 in MSK", W, ["msk"]),
    ("orders on 15 March 2017 at 12:00 Z", W, ["12", "00", "z"]),
    # A caller's hours consume nothing: the question's hours are left over, whatever they are,
    # and so is its date, which a window shorter than the day is not.
    ("orders on 15 March 2017", _draft(time={"grain": "day", **HOUR}), ["15", "2017"]),
    (
        "revenue from 12:00 to 13:00 on 15 March 2017",
        _draft(time={"grain": "day", **HOUR}),
        ["12", "00", "13", "15", "2017"],
    ),
    (
        "revenue from 9 to 17 on 15 March 2017",
        _draft(time={"grain": "day", **HOUR}),
        ["9", "17", "15", "2017"],
    ),
    # A caller's window is not a zone.
    (
        "orders from 12:00 to 13:00 UTC",
        _draft(time={"grain": "day", **HOUR}),
        ["12", "00", "13", "utc"],
    ),
    # A count and a threshold in one question: each is read where it is stated.
    (
        "the 2 stores with the most revenue among those with at\nleast 10 orders",
        _draft(limit=2, having=_over(10, "gte")),
        [],
    ),
    (
        "the 2 stores with the most revenue among those with at\nleast 10 orders",
        _draft(limit=2),
        ["10"],
    ),
    # A "first N" is not a ranking plan reads, so no construct states its number: refused.
    ("first 10 customers by revenue", _draft(limit=10), ["10"]),
    # A threshold that repeats the limit's number is not the limit, and the limit is not it.
    ("top 10 stores by revenue with at least 10 orders", _draft(limit=10), ["10"]),
    (
        "top 10 stores by revenue with at least 10 orders",
        _draft(limit=10, having=_over(10, "gte")),
        [],
    ),
    (
        "top 10 stores by revenue with at least 10 orders",
        _draft(having=_over(10, "gte")),
        ["10"],
    ),
    ("the 10 largest stores by revenue", _draft(limit=10), []),
    # A number is a percentage only before %, percent or percentile.
    ("stores with revenue over 500", _draft(having=_over(5)), ["500"]),
    ("top 1000 customers by revenue", _draft(limit=10), ["1000"]),
    ("top 100 customers by revenue", _draft(limit=1), ["100"]),
    ("stores in the top 10 percent", _draft(having=_over(0.9)), []),
    ("stores over 50 percent of revenue", _draft(having=_over(0.5)), []),
    ("stores over 50% of revenue", _draft(having=_over(0.5)), []),
    ("stores over 50 of revenue", _draft(having=_over(0.5)), ["50"]),
    ("stores in the 10 of revenue", _draft(having=_over(0.9)), ["10"]),
    # A year-shaped clock time is never the year of the caller's bounds, nor an exclusive end
    # year the bounds carry; a bare year no date phrase states is left over too.
    ("revenue in 2017 at 2000", _caller(YEAR_2017), ["2000"]),
    ("revenue in 2017 at 1930", _caller(YEAR_2017), ["1930"]),
    ("revenue in 2017 at 2017", _caller(YEAR_2017), ["2017"]),
    ("revenue in 2017 at 2018", _caller(YEAR_2017), ["2018"]),
    ("revenue 2018", _caller(YEAR_2017), ["2018"]),
    ("revenue 2017", _caller(YEAR_2017), ["2017"]),
    (
        "revenue on 15 March 2017 from 1900 to 2000 hours",
        _caller(HOUR),
        ["15", "2017", "1900", "2000", "hours"],
    ),
    (
        "revenue from 9:30 to 17:00 on 15 March 2017",
        _caller(HOUR),
        ["9", "30", "17", "00", "15", "2017"],
    ),
    # A day's bounds answer no clock word...
    ("revenue from noon to midnight on 15 March 2017", _caller(DAY_BOUNDS), ["noon", "midnight"]),
    ("revenue from noon to midnight on 15 March 2017", _caller(DATE_BOUNDS), ["noon", "midnight"]),
    ("revenue on 15 March 2017 for 3 hours", _caller(DAY_BOUNDS), ["3", "hours"]),
    ("revenue on 15 March 2017 at 0", _caller(DAY_BOUNDS), ["0"]),
    # ...and neither do bounds that state hours: no clock time is consumed by its value,
    # whether it matches a bound, is reversed, or sits in another role. Such bounds are not
    # the day the question states either, so its date is left over too.
    (
        "revenue from noon to midnight on 15 March 2017",
        _caller(NOON_TO_MIDNIGHT),
        ["noon", "midnight", "15", "2017"],
    ),
    (
        "revenue from 9:30 am to 5 pm on 15 March 2017",
        _caller(BUSINESS_HOURS),
        ["9", "30", "5", "15", "2017"],
    ),
    (
        "revenue from 09:30 to 17:00 on 15 March 2017",
        _caller(BUSINESS_HOURS),
        ["09", "30", "17", "00", "15", "2017"],
    ),
    (
        "revenue on 15 March 2017 from 17:00 to 09:30",
        _caller(BUSINESS_HOURS),
        ["15", "2017", "17", "00", "09", "30"],
    ),
    (
        "revenue on 15 March 2017 except 17:00-09:30",
        _caller(BUSINESS_HOURS),
        ["15", "2017", "17", "00", "09", "30"],
    ),
    (
        "revenue on 15 March 2017 from 9:30 to 18:00",
        _caller(BUSINESS_HOURS),
        ["15", "2017", "9", "30", "18", "00"],
    ),
    (
        "revenue on 15 March 2017 at 17 or 1700",
        _caller(BUSINESS_HOURS),
        ["15", "2017", "17", "1700"],
    ),
    (
        "revenue on 15 March 2017 at 9 o'clock",
        _caller(BUSINESS_HOURS),
        ["15", "2017", "9", "clock"],
    ),
    ("revenue on 15 March 2017 from 9 to 17", _caller(BUSINESS_HOURS), ["15", "2017", "9", "17"]),
    (
        "revenue on 15 March 2017 per hour, 17:00",
        _caller(BUSINESS_HOURS),
        ["15", "2017", "hour", "17", "00"],
    ),
    (
        "revenue on 15 March 2017 at 17:00 UTC",
        _caller(BUSINESS_HOURS),
        ["15", "2017", "17", "00", "utc"],
    ),
    ("revenue on 15 March 2017", _caller(BUSINESS_HOURS), ["15", "2017"]),
]


@pytest.mark.parametrize(("text", "query", "terms"), CONSUMED)
def test_a_term_is_consumed_only_by_the_span_of_the_construct_that_reads_it(
    runtime: Runtime, text: str, query: dict[str, Any], terms: list[str]
) -> None:
    assert unconsumed_terms(runtime, text, query) == terms


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        ("top 5 stores by revenue", 5),
        ("the top 5 stores by revenue", 5),
        ("bottom 3 stores by revenue", 3),
        ("top-3 stores by revenue", 3),
        ("best 3 stores by revenue", 3),
        ("5 best-selling products", 5),
        ("the 3 lowest-selling products by item revenue", 3),
        ("which 3 stores have the highest revenue", 3),
        ("which 5 stores had the most orders", 5),
        # A cue the number has no cue beside: the ranking parser reads the count.
        ("the 5 customers who spent the most", 5),
        ("3 stores with the highest revenue", 3),
        ("the 3 stores with the least revenue", 3),
        ("top 2000 customers by lifetime spend", 2000),
    ],
)
def test_a_ranking_consumes_its_count_by_span(runtime: Runtime, text: str, limit: int) -> None:
    assert unconsumed_terms(runtime, text, _draft(limit=limit)) == []
    # The count is the limit's text only where the ranking states it, and only the draft's.
    assert unconsumed_terms(runtime, text, _draft(limit=limit + 1)) == [str(limit)]
    assert unconsumed_terms(runtime, text, _draft()) == [str(limit)]
    assert unconsumed_terms(runtime, f"{text} at {limit}", _draft(limit=limit)) == [str(limit)]


@dataclass
class _Row:
    id: str
    name: str = ""
    label: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    calendar_id: str = ""


def _runtime_with(*rows: _Row) -> Any:
    config = SimpleNamespace(
        measures=list(rows),
        metric_recipes=[],
        dimensions=[],
        entities=[],
        segments=[],
        temporal_roles=[],
        value_domains=[],
    )
    return SimpleNamespace(_config=config)


@pytest.mark.parametrize(
    ("text", "row", "terms"),
    [
        # An object's description consumes nothing: only its own names do.
        (
            "revenue per hour on 15 March 2017",
            _Row(
                "measure.x.revenue", "revenue", "Revenue", "Revenue per hour over the last 30 days"
            ),
            ["hour"],
        ),
        (
            "revenue at 30 on 15 March 2017",
            _Row("measure.x.revenue", "revenue", "Revenue", "Revenue over 30 days"),
            ["30"],
        ),
        # Its name, label or alias does, where the question spells it.
        (
            "orders per hour on 15 March 2017",
            _Row("measure.x.orders_per_hour", "orders_per_hour", "Orders per hour"),
            [],
        ),
        (
            "30 day retention on 15 March 2017",
            _Row("measure.x.retention", "retention", "Retention", aliases=["30 day retention"]),
            [],
        ),
        (
            "p95 latency on 15 March 2017",
            _Row("measure.x.p95_latency", "p95_latency", "P95 latency"),
            [],
        ),
    ],
)
def test_an_object_consumes_the_words_of_its_own_names_only(
    text: str, row: _Row, terms: list[str]
) -> None:
    query = _draft(row.id, time=WINDOW)
    assert unconsumed_terms(_runtime_with(row), text, query) == terms


def test_a_question_past_the_scan_cap_is_still_held_to_the_rule(runtime: Runtime) -> None:
    letters = "abcdefghijklmnopqrstuvwxyz"
    filler = " ".join(a + b for a in letters for b in letters if a + b != "tz")
    text = f"revenue on 15 March 2017 {filler[:1800]} at 14h30"
    assert len(text) < 2000
    assert unconsumed_terms(runtime, text, _draft(time=WINDOW)) == ["14h30"]
    # Too long to read, only the years its date phrases state are consumed.
    text = f"revenue in 2017 {filler[:2100]} at 1930 or 14h30 or 2000"
    assert len(text) > 2000
    assert unconsumed_terms(runtime, text, _caller(YEAR_2017)) == ["1930", "14h30", "2000"]
    # Without a caller window nothing consumes the year either.
    assert unconsumed_terms(runtime, text, _draft()) == ["2017", "1930", "14h30", "2000"]


@pytest.mark.parametrize("huge", [10**400, -(10**400)])
def test_a_number_too_large_to_read_is_not_a_crash(runtime: Runtime, huge: int) -> None:
    query = _draft(limit=huge, having=_over(huge), time=WINDOW)
    assert unconsumed_terms(runtime, f"top 5 stores over {'9' * 400}", query) == ["5", "9" * 400]
    assert unmatched_intent_terms(runtime, "revenue over 5", Call.from_call(query)) == ["5"]


# A token that looks like a number but is not one: a dotted date, a version, an address.
ODD_NUMERALS = [
    "15.03.2017",
    "1.234.567",
    "1,234,567",
    "10.0.0.1",
    "1.2.3",
    "1,2,3",
    "1.2,3.4",
    "0.0.0",
    "1..2",
    "1.",
    ".5",
    "1e5",
    "5th",
    "3rd.4th",
    "٣.٤.٥",
    "１２.３４.５６",
    "²",
    "1" * 400,
    "1." * 200 + "1",
]


@pytest.mark.parametrize("token", ODD_NUMERALS)
def test_a_numeral_that_is_not_one_number_is_never_a_crash(runtime: Runtime, token: str) -> None:
    for query in (_draft(), _draft(time=WINDOW, limit=5, having=_over(5))):
        for text in (
            f"revenue on 15 March 2017 for {token}",
            f"{token} orders over {token}",
            f"top 5 stores by revenue in 2017 version {token}",
            token,
        ):
            assert isinstance(unmatched_intent_terms(runtime, text, Call.from_call(query)), list)
            assert isinstance(unconsumed_terms(runtime, text, query), list)


def test_a_customer_ranking_keeps_its_window_and_ordinary_customer_key(runtime: Runtime) -> None:
    payload = _plan(runtime, "top 10 customers by revenue in Q1 2017")
    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]
    assert payload["best"]["query"]["group_by"] == ["dimension.jaffle_customer_id"]
    time = payload["best"]["query"]["time"]
    assert (time["start"], time["end"]) == ("2017-01-01", "2017-04-01")


@pytest.mark.parametrize(
    ("text", "time", "status", "code"),
    [
        # A window shorter than a day the caller states is read as is.
        ("revenue for the last 24 hours", HOUR, "ok", None),
        ("orders", HOUR, "ok", None),
        # plan reads a caller's bounds only at whole days, so hours are not the day a question
        # names, and hours the caller's window doesn't state are left over.
        ("orders on 15 March 2017", HOUR, "low_confidence", None),
        ("revenue on 15 March 2017", HOUR, "low_confidence", "PLAN_INTENT_COVERAGE_GAP"),
        (
            "revenue from 12:00 to 13:00 on 15 March 2017",
            HOUR,
            "low_confidence",
            "PLAN_INTENT_COVERAGE_GAP",
        ),
        (
            "revenue from 9 to 17 on 15 March 2017",
            HOUR,
            "low_confidence",
            "PLAN_INTENT_COVERAGE_GAP",
        ),
        # A whole day answers no clock word.
        ("revenue from noon to midnight on 15 March 2017", DAY_BOUNDS, "low_confidence", None),
        ("revenue from noon to midnight on 15 March 2017", DATE_BOUNDS, "low_confidence", None),
        # Nor is a year-shaped clock time the year of the caller's bounds.
        ("revenue in 2017 at 2000", YEAR_2017, "low_confidence", None),
        ("revenue in 2017 at 1930", YEAR_2017, "low_confidence", None),
        ("revenue in 2017 at 2017", YEAR_2017, "low_confidence", None),
        ("revenue in 2017 at 2018", YEAR_2017, "low_confidence", None),
        ("revenue 2018", YEAR_2017, "low_confidence", None),
        ("revenue 2017", YEAR_2017, "low_confidence", None),
        ("revenue on 15 March 2017 from 1900 to 2000 hours", HOUR, "low_confidence", None),
        ("revenue from 9:30 to 17:00 on 15 March 2017", HOUR, "low_confidence", None),
        # Another day than the question's.
        ("revenue on 15 March 2017", SOMEONE_ELSES_DAY, "low_confidence", None),
        ("revenue in March 2017", SOMEONE_ELSES_DAY, "low_confidence", None),
        ("revenue in 2017", SOMEONE_ELSES_DAY, "low_confidence", None),
    ],
)
def test_plan_reads_a_callers_window_against_the_question(
    runtime: Runtime, text: str, time: dict[str, str], status: str, code: str | None
) -> None:
    partial = {"time": {"temporal_role": ORDER_TIME, "grain": "day", **time}}
    payload = _plan(runtime, text, partial_query=partial)
    if status == "ok":
        assert payload["status"] == "ok", payload.get("why")
        assert {key: _query(payload)["time"][key] for key in ("start", "end")} == time
        return
    _held(payload, *([code] if code else []))
    if "noon" in text:
        assert payload["why"]["details"]["terms"] == ["noon", "midnight"]
    if "9 to 17" in text:
        assert payload["warnings"][0]["details"]["terms"] == ["9", "17"]


def test_plan_refuses_an_hour_on_a_date_role_too(runtime_factory: Any) -> None:
    # The invariant doesn't depend on the role's column type, so a date column never gets a
    # timestamp bound it can't compare.
    runtime = runtime_factory("tpch_sf1_showcase")
    try:
        payload = _plan(runtime, "orders from 12:00 to 13:00 on 15 March 1995")
        _held(payload, "PLAN_UNMATCHED_TERMS", "TIME_WINDOW_UNRESOLVED")
    finally:
        runtime.close()


def test_a_day_is_still_resolved_as_a_day() -> None:
    window = _time_window("revenue on 15 March 2017")
    assert window.bounds == DATE_BOUNDS
    assert window.unresolved == () and window.assumptions == ()


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


def test_plan_names_the_two_windows_that_differ(runtime: Runtime) -> None:
    ok = plan_payload(
        runtime, intent="revenue in Q1 2017 (January 1 to March 31, 2017)", detail="query"
    )
    assert ok["status"] == "ok"
    # No grain is asked for, so the window is one total.
    assert "grain" not in _query(ok)["time"]
    assert {key: _query(ok)["time"][key] for key in ("start", "end")} == Q1_2017
    assert "time.end is 2017-04-01" in ok["assumptions"][0]
    clash = plan_payload(
        runtime, intent="revenue in Q1 2017 (January 1 to March 30, 2017)", detail="query"
    )
    assert clash["status"] == "low_confidence"
    assert clash["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert len(clash["why"]["details"]["conflicting_phrases"]) == 2
    assert "query" not in clash["best"]


# --- window forms -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("revenue in the first half of 2017", {"start": "2017-01-01", "end": "2017-07-01"}),
        ("revenue in the second half of 2017", {"start": "2017-07-01", "end": "2018-01-01"}),
        ("revenue for first half 2017", {"start": "2017-01-01", "end": "2017-07-01"}),
        ("revenue from March 1 to March 31, 2017", MARCH_2017),
        ("revenue from March 1 to 31, 2017", MARCH_2017),
        ("revenue Mar 1 - Mar 31 2017", MARCH_2017),
        ("revenue Mar 1 - 31 2017", MARCH_2017),
        ("revenue between 2017-03-01 and 2017-03-31", MARCH_2017),
        ("revenue 2017-03-01 to 2017-03-31", MARCH_2017),
        ("revenue from 2017-03-01 through 2017-03-31", MARCH_2017),
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
    ("text", "measure", "unconsumed"),
    [
        ("item revenue in 2017", "measure.jaffle.item_revenue_usd", []),
        ("total item revenue by month", "measure.jaffle.item_revenue_usd", []),
        ("Item revenue (USD) in 2017", "measure.jaffle.item_revenue_usd", []),
        ("revenue in 2017", "measure.jaffle.revenue_usd", []),
        # The words are not a name when they are not adjacent, or name another thing. "item" names
        # the Item entity, and validation refuses revenue, read per order, by an item's key.
        ("revenue by item in 2017", "measure.jaffle.revenue_usd", None),
        ("food revenue in 2017", "measure.jaffle.food_revenue_usd", []),
        # A name inside other words is not the ask: this is revenue, not the count of large orders.
        # The draft doesn't filter on the large-order flag, so it isn't ready.
        ("large order revenue by month", "measure.jaffle.revenue_usd", ["large"]),
        ("large orders by month", "measure.jaffle.large_order_count", []),
        # A single measure still plans.
        ("revenue in Q1 2017", "measure.jaffle.revenue_usd", []),
        ("revenue by store for the first half of 2017", "measure.jaffle.revenue_usd", []),
    ],
)
def test_an_exact_multi_word_label_outranks_a_partial_one(
    runtime: Runtime, text: str, measure: str, unconsumed: list[str] | None
) -> None:
    payload = plan_payload(runtime, intent=text, detail="query")
    if unconsumed is None:
        # Revenue, read per order, can't reach an item: plan names the pair, no draft.
        assert payload["status"] == "unrealizable", payload
        assert payload["why"]["code"] == "PLAN_UNCONNECTED"
        assert payload["why"]["details"]["subjects"] == [measure]
        return
    assert _measures(payload) == [measure]
    assert payload["status"] == ("low_confidence" if unconsumed else "ok"), payload.get("why")
    assert (payload.get("why") or {}).get("details", {}).get("terms", []) == unconsumed


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
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, text: str, replaces_target: bool
) -> None:
    asked: list[str] = []
    real = rollup._named_measure
    monkeypatch.setattr(
        rollup,
        "_named_measure",
        lambda *args, **kwargs: asked.append(text) or real(*args, **kwargs),
    )
    assert rollup._match(runtime, text, set(_tokens(text))) is not None
    assert bool(asked) is replaces_target


def test_a_name_is_the_longest_exact_one(runtime: Runtime) -> None:
    config = runtime._config
    assert _named_measure(config, "item revenue by month").id == "measure.jaffle.item_revenue_usd"
    assert _named_measure(config, "gross profit in 2017").id == "measure.jaffle.gross_profit_usd"
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
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str, missing: str | tuple[str, ...]
) -> None:
    monkeypatch.setattr(runtime, "_config", runtime._config)  # restored after the test
    disable_planner_patterns(runtime, "conjoined_metrics")
    payload = plan_payload(runtime, intent=intent, detail="query")
    _held(payload, "PLAN_INTENT_COVERAGE_GAP")
    gaps = [gap for gap in payload["why"]["details"]["gaps"] if "subjects" in gap["kind"]]
    assert gaps, payload["why"]
    named = {row["phrase"] for gap in gaps for row in gap["actual"]["missing"]}
    assert named & set((missing,) if isinstance(missing, str) else missing), gaps


def test_conjoined_store_grouping_answers_with_the_keyed_store(runtime: Runtime) -> None:
    # "store" names the Store entity: both subjects group by its key and its name.
    payload = plan_payload(runtime, intent="revenue and orders by store", detail="query")
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    assert query["group_by"] == ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]
    rows = runtime.query(query)["rows"]
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        reference = connection.execute(
            "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0), "
            "COUNT(DISTINCT o.order_id) FROM jaffle_order o JOIN jaffle_store s "
            "USING (store_id) GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
    actual = [
        (
            row["dimension.jaffle_store_id"],
            row["dimension.jaffle_store_name"],
            round(float(row["revenue_usd"]), 2),
            row["order_count"],
        )
        for row in rows
    ]
    assert actual == [
        (key, name, round(float(revenue), 2), count) for key, name, revenue, count in reference
    ]


# --- unmatched terms ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "query", "expected"),
    [
        # Function words and verbs about how a value is worded are not unmatched.
        ("revenue dated in 2017", _draft(), []),
        ("revenue placed only in 2017", _draft(), []),
        ("revenue counted using the order time", _draft(), []),
        ("revenue that came in during 2017 while using such", _draft(), []),
        ("revenue anchored on the order time", _draft(), []),
        ("revenue placed only in 2017 while using order time counted such as dated", _draft(), []),
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
        # A number is read by span: "5" equals the draft's limit but no ranking states it there,
        # and a threshold the draft doesn't carry doesn't either.
        ("revenue over 5", _draft(limit=5), ["5"]),
        ("top 5 stores by revenue", _draft(limit=5), ["stores"]),
        ("revenue over 5", _draft(having=_over(5)), []),
        ("top 5 stores by revenue", _draft(having=_over(5)), ["5", "stores"]),
    ],
)
def test_unmatched_words_are_the_ones_the_draft_does_not_carry(
    runtime: Runtime, question: str, query: dict[str, Any], expected: list[str]
) -> None:
    assert unmatched_intent_terms(runtime, question, Call.from_call(query)) == expected


@pytest.mark.parametrize(
    ("question", "query"),
    [
        # A number the draft carries: a limit, a threshold, a percentage.
        ("top 5 stores by revenue", _draft(limit=5)),
        ("stores with revenue over 500 dollars", _draft(having=_over(500))),
        ("stores with more than 50 percent of revenue", _draft(having=_over(0.5))),
        ("orders in the 7 days", _draft()),
        # A year is the draft's window, whoever set it.
        ("revenue for 2017 please", _draft(time={"grain": "year", **YEAR_2017})),
        ("revenue last 30 days", _draft()),
    ],
)
def test_numbers_the_draft_carries_are_matched(
    runtime: Runtime, question: str, query: dict[str, Any]
) -> None:
    assert not {"5", "500", "50", "2017", "30"} & set(
        unmatched_intent_terms(runtime, question, Call.from_call(query))
    )


# --- names the draft dropped -----------------------------------------------------------


@pytest.mark.parametrize(
    "intent",
    [
        "item revenue by product for tangaroo and vanilla ice in 2017",
        "combined item revenue from doctor stew, the krautback and mel-bun by quarter",
    ],
)
def test_a_list_of_names_the_draft_does_not_filter_on_is_not_ok(
    runtime: Runtime, intent: str
) -> None:
    payload = plan_payload(runtime, intent=intent, detail="query")
    assert _measures(payload) == ["measure.jaffle.item_revenue_usd"]
    _held(payload, "PLAN_UNMATCHED_TERMS")
    assert len(payload["why"]["details"]["terms"]) >= 2
    assert payload["warnings"][0]["details"]["terms"] == payload["why"]["details"]["terms"]


def test_a_single_comparison_word_asks_how_to_read_the_comparison(runtime: Runtime) -> None:
    # "YoY" asks for two years side by side, which the plan doesn't answer as asked: it names
    # the comparison and offers the years read as one monthly series. ("revenue with
    # cumulative" is not one: "cumulative" names the Cumulative revenue metric.)
    payload = plan_payload(runtime, intent="monthly revenue with YoY", detail="query")
    assert payload["status"] == "needs_clarification", payload.get("why")
    assert payload["why"]["code"] == "PERIOD_COMPARISON"
    assert payload["why"]["details"]["options"]
    assert _measures(payload)[0] == "measure.jaffle.revenue_usd"
    assert "execute" not in (payload.get("next") or {}).get("ready_for", [])


def test_a_lifetime_percentile_cohort_is_refused_not_warned(runtime: Runtime) -> None:
    """A percentile cohort cut by a lifetime balance is held, not ok: the balance is read
    through a predicate on no stated day."""
    payload = plan_payload(
        runtime, intent="revenue from top decile of customers by lifetime spend", detail="query"
    )
    _held(payload, "PLAN_INTENT_COVERAGE_GAP")
    assert [gap["kind"] for gap in payload["why"]["details"]["gaps"]] == ["stock_as_of_unrealized"]
    # A top decile is a share of customers, not a ranking: the draft carries no limit.
    assert "limit" not in payload["best"]["query"]


# --- a window in the draft must agree with the question's date phrases -------------------


def _last_days(days: int) -> dict[str, str]:
    end = datetime.now(UTC).date()
    return {"start": (end - timedelta(days=days)).isoformat(), "end": end.isoformat()}


@pytest.mark.parametrize(
    ("text", "time", "agrees"),
    [
        # The same day, as a whole day: a date, or that date at midnight. Hours within it, or a
        # reversed or empty window, are not the day.
        ("revenue on 15 March 2017", DATE_BOUNDS, True),
        ("revenue on 15 March 2017", {"start": f"{MARCH_15}T00:00:00", "end": "2017-03-16"}, True),
        ("revenue on 15 March 2017", HOUR, False),
        ("revenue on 15 March 2017", {"start": f"{MARCH_15}T12:00:00", "end": "2017-03-16"}, False),
        ("revenue on 15 March 2017", {"start": "2017-03-16", "end": MARCH_15}, False),
        (
            "revenue on 15 March 2017",
            {"start": f"{MARCH_15}T12:00", "end": f"{MARCH_15}T12:00"},
            False,
        ),
        ("revenue in March 2017", MARCH_2017, True),
        ("revenue in Q1 2017", Q1_2017, True),
        ("revenue in 2017", YEAR_2017, True),
        ("revenue for the last 7 days", {"last": {"unit": "day", "value": 7}}, True),
        ("revenue for the last 7 days", _last_days(7), True),
        # A window missing a bound reads past the question's on that side.
        ("revenue in 2017", {"end": "2018-01-01"}, False),
        ("revenue in 2017", {"start": "2017-01-01"}, False),
        ("revenue in 2017", {"start": "2018-01-01"}, False),
        ("revenue on 15 March 2017", {"end": "2017-03-16T13:00:00"}, False),
        # Another day, or the same start with another end.
        ("revenue on 15 March 2017", SOMEONE_ELSES_DAY, False),
        ("revenue on 15 March 2017", {"start": MARCH_15, "end": "2017-03-17"}, False),
        # Finer than the phrase's grain, or coarser.
        ("revenue in March 2017", DATE_BOUNDS, False),
        ("revenue in Q1 2017", YEAR_2017, False),
        ("revenue in 2017", MARCH_2017, False),
        # A year the window's bounds carry is not the year the question states.
        ("revenue in 2018", YEAR_2017, False),
        ("revenue for the last 7 days", {"last": {"unit": "day", "value": 30}}, False),
        ("revenue for the last 7 days", _last_days(30), False),
        # Bounds that are not dates never agree.
        ("revenue on 15 March 2017", {"start": "soon", "end": "later"}, False),
        # A question that states no window agrees with any.
        ("revenue by store", SOMEONE_ELSES_DAY, True),
    ],
)
def test_a_window_agrees_with_the_date_phrases_only_at_the_phrases_start_and_end(
    runtime: Runtime, text: str, time: dict[str, str], agrees: bool
) -> None:
    query = _caller(time)
    assert (_caller_window_gaps(runtime, text, Call.from_call(query)) == []) is agrees
    # What disagrees consumes none of the phrase, so its numbers are left over.
    left = unconsumed_terms(runtime, text, query)
    assert (left == []) is (agrees or not any(char.isdigit() for char in text))


def test_a_window_over_two_phrases_must_span_them_and_no_more(runtime: Runtime) -> None:
    text = "revenue in Q1 2017 and in Q3 2017"
    both = {"start": "2017-01-01", "end": "2017-10-01"}
    for time, agrees in ((both, True), (Q1_2017, False), ({**both, "end": "2018-01-01"}, False)):
        gaps = _caller_window_gaps(runtime, text, Call.from_call(_caller(time)))
        assert (gaps == []) is agrees, time


LONG_FILLER = " ".join(f"word{index}" for index in range(400))


def test_a_question_too_long_to_read_is_held_to_the_years_its_date_phrases_state(
    runtime: Runtime,
) -> None:
    assert len(LONG_FILLER) > 2000
    query = Call.from_call(_caller(YEAR_2017))
    for year, agrees in ((2017, True), (2018, False)):
        gaps = _caller_window_gaps(runtime, f"revenue in {year} {LONG_FILLER}", query)
        assert (gaps == []) is agrees


@pytest.mark.parametrize(
    ("text", "left"),
    [
        # A quantity after "for" is not a second window, and no year is told from it here: the
        # years are left unconsumed, so the plan is not ready.
        (f"revenue in 2017 for 2000 customers {LONG_FILLER}", "2017"),
        (f"revenue for 2000 customers in 2017 {LONG_FILLER}", "2017"),
        # "2017" is the stated window; the count is a number no construct reads.
        (f"revenue in 2017 {LONG_FILLER} in 2000 or more orders", "2000"),
    ],
    ids=["quantity-after-year", "quantity-before-year", "count-after-filler"],
)
def test_a_question_too_long_to_read_never_reads_a_quantity_as_a_window(
    runtime: Runtime, text: str, left: str
) -> None:
    query = _caller(YEAR_2017)
    # No window is stated, so none is invented for the caller's to disagree with.
    assert _caller_window_gaps(runtime, text, Call.from_call(query)) == []
    leftover = unconsumed_terms(runtime, text, query)
    assert left in leftover
    if left == "2000":
        assert "2017" not in leftover
