"""plan resolves clear calendar windows, reports the rest, and buckets totals once.

A question scoped to "in 2017", "the first half of 2017" or "April 1 to April
7, 2017" used to lose its window silently, so the draft answered an unbounded
question with status ok. The planner now resolves a window only when the
question names exactly one, in a form it reads unambiguously. A bound ("before
2017", "since March 2017"), a qualifier ("early 2017", "the end of 2017"), a
comparison year, a numeric date or two windows at once is reported as
unresolved instead of being narrowed or widened to the nearest form that
parses. A total over a window gets a grain that yields a single bucket.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import (
    _time_bounds_from_text,
    _time_spec,
    _unresolved_time_phrases,
)

YEAR_2017 = {"start": "2017-01-01", "end": "2018-01-01"}
Q2_2017 = {"start": "2017-04-01", "end": "2017-07-01"}
APRIL_3 = {"start": "2017-04-03", "end": "2017-04-04"}
FIRST_WEEK_OF_APRIL = {"start": "2017-04-01", "end": "2017-04-08"}


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("total revenue in 2017", YEAR_2017),
        ("monthly revenue by store for 2017", YEAR_2017),
        ("orders during the year 2017", YEAR_2017),
        ("revenue in 2016 and 2017", {"start": "2016-01-01", "end": "2018-01-01"}),
        ("compare revenue between 2016 and 2017", {"start": "2016-01-01", "end": "2018-01-01"}),
        ("revenue 2016-2017", {"start": "2016-01-01", "end": "2018-01-01"}),
        ("gross profit in the first half of 2017", {"start": "2017-01-01", "end": "2017-07-01"}),
        ("H2 2017 orders", {"start": "2017-07-01", "end": "2018-01-01"}),
        ("revenue by store in Q2 2017", Q2_2017),
        ("revenue in Q2 of 2017", Q2_2017),
        ("2017 Q2 revenue", Q2_2017),
        ("revenue for the second quarter of 2017", Q2_2017),
        ("orders in March 2017", {"start": "2017-03-01", "end": "2017-04-01"}),
        ("orders in March of 2017", {"start": "2017-03-01", "end": "2017-04-01"}),
        ("item revenue from March through May 2017", {"start": "2017-03-01", "end": "2017-06-01"}),
        ("orders from April 1 to April 7, 2017", FIRST_WEEK_OF_APRIL),
        # The year may sit on either end of a day range.
        ("orders from April 1, 2017 to April 7", FIRST_WEEK_OF_APRIL),
        ("orders April 1-7, 2017", FIRST_WEEK_OF_APRIL),
        ("orders between March 30 and April 2, 2017", {"start": "2017-03-30", "end": "2017-04-03"}),
        ("revenue on April 3, 2017", APRIL_3),
        ("revenue on the 3rd of April, 2017", APRIL_3),
        ("orders on 2017-04-03", APRIL_3),
        ("orders between 2017-04-01 and 2017-04-07", FIRST_WEEK_OF_APRIL),
        (
            "orders from December 30, 2016 to January 2, 2017",
            {"start": "2016-12-30", "end": "2017-01-03"},
        ),
    ],
)
def test_clear_calendar_windows_resolve(text: str, bounds: dict[str, str]) -> None:
    assert _time_bounds_from_text(text) == bounds
    assert _unresolved_time_phrases(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "top 2000 customers",
        "orders over 2000",
        "orders over $2000",
        "orders > 2000",
        "at least 2050 items",
        "customers with 2000 or more orders",
        "the first 2000 orders",
        "revenue for store 2045",
    ],
)
def test_quantities_are_not_years(text: str) -> None:
    assert _time_bounds_from_text(text) == {}
    assert _unresolved_time_phrases(text) == []


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        # Bounds are not windows.
        ("revenue before 2017", "before 2017"),
        ("revenue after 2016", "after 2016"),
        ("pre-2017 revenue", "pre-2017"),
        ("revenue since 2016", "since 2016"),
        ("orders since March 2017", "since march 2017"),
        ("orders before April 3, 2017", "before april 3, 2017"),
        ("orders from March 1, 2017 onward", "from march 1, 2017"),
        ("revenue as of June 30, 2017", "as of june 30, 2017"),
        ("orders by June 30, 2017", "by june 30, 2017"),
        ("2 weeks ending April 14, 2017", "ending april 14, 2017"),
        # Qualified windows don't widen to their year or narrow to their day.
        ("revenue in early 2017", "early 2017"),
        ("revenue in the last half of 2017", "last half"),
        ("orders in the first six months of 2017", "of 2017"),
        ("orders in the last few weeks of 2017", "last few weeks"),
        ("Christmas 2016 revenue", "christmas 2016"),
        ("orders in the week of April 3, 2017", "of april 3, 2017"),
        # A year needs a scoping preposition to resolve on its own.
        ("2017 revenue by store", "2017"),
        # Comparisons, conflicts and gaps.
        ("revenue 2017 vs 2016", "vs 2016"),
        ("revenue in 2017 relative to 2016", "relative to 2016"),
        ("revenue in 2015 and 2017", "2015 and 2017"),
        ("revenue from 2016 until today", "from 2016"),
        ("revenue in the last 3 months of 2017", "last 3 months"),
        ("orders from Jan 1 2017 to Mar 2017", "from jan 1 2017"),
        ("orders from April 7 to April 1, 2017", "april 7 to april 1, 2017"),
        # Dates it can't read unambiguously, or at all.
        ("orders on 4/3/2017", "4/3/2017"),
        ("revenue on Feb 30, 2017", "feb 30, 2017"),
        ("revenue for Q2", "q2"),
        ("revenue in March", "in march"),
    ],
)
def test_unclear_windows_are_reported(text: str, phrase: str) -> None:
    assert _time_bounds_from_text(text) == {}
    phrases = _unresolved_time_phrases(text)
    assert any(phrase in reported for reported in phrases), phrases


@pytest.mark.parametrize(
    ("text", "grain"),
    [
        ("total revenue in 2017", "year"),
        ("gross profit in the first half of 2017", "year"),
        ("item revenue from March through May 2017", "year"),
        ("orders in March 2017", "month"),
        ("revenue by store in Q2 2017", "quarter"),
        ("revenue on April 3, 2017", "day"),
        # April 1, 2017 is a Saturday, so the first seven days are no calendar week...
        ("orders from April 1 to April 7, 2017", "month"),
        # ...while Monday July 3 to Sunday July 9 is one.
        ("orders from July 3 to July 9, 2017", "week"),
        ("revenue in 2016 and 2017", "year"),
        # An explicit grain or a trend cue still wins.
        ("monthly revenue by store for 2017", "month"),
        ("daily orders from April 1 to April 7, 2017", "day"),
        ("revenue trend in 2017", "month"),
        ("revenue by month", "month"),
    ],
)
def test_totals_over_a_window_get_one_bucket(text: str, grain: str) -> None:
    assert _time_spec("temporal_role.jaffle_order_time", text)["grain"] == grain


def _best(payload: dict[str, Any]) -> dict[str, Any]:
    return (payload.get("best") or {}).get("query_ir") or {}


def test_plan_keeps_the_year(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        total = plan_payload(runtime, intent="total revenue in 2017", detail="query")
        assert total["status"] == "ok"
        assert _best(total)["time"] == {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "year",
            **YEAR_2017,
        }
        trend = plan_payload(runtime, intent="monthly revenue by store for 2017", detail="query")
        assert trend["status"] == "ok"
        assert {key: _best(trend)["time"][key] for key in ("grain", "start", "end")} == {
            "grain": "month",
            **YEAR_2017,
        }
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "intent", ["revenue 2017 vs 2016", "orders since March 2017", "revenue before 2017"]
)
def test_plan_reports_a_window_it_cannot_resolve(runtime_factory: Any, intent: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent, detail="query")
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
        assert not {"start", "end"} & set(_best(payload).get("time") or {})
    finally:
        runtime.close()


def test_a_callers_explicit_window_settles_the_question(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    caller_window = {"time": {"start": "2017-03-01", "end": "2018-01-01"}}
    try:
        payload = plan_payload(
            runtime, intent="orders since March 2017", partial_query=caller_window, detail="query"
        )
    finally:
        runtime.close()
    assert (payload.get("why") or {}).get("code") != "TIME_WINDOW_UNRESOLVED"
    assert _best(payload)["time"]["start"] == "2017-03-01"


def test_a_lookback_metric_keeps_its_end_and_says_so(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime, intent="month-over-month revenue growth by month in 2017", detail="query"
        )
        time = _best(payload)["time"]
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "TIME_WINDOW_START_DROPPED"
        assert payload["why"]["details"]["requested_start"] == "2017-01-01"
        assert "start" not in time and time["end"] == "2018-01-01"
        # The draft runs: the engine can't bound this metric's start, but it can its end.
        from semantic_rails.mcp import SemanticLayerMCPAdapter

        adapter = SemanticLayerMCPAdapter(runtime)
        assert adapter.call_tool("validate", {"query": _best(payload)})["ok"] is True
    finally:
        runtime.close()


def test_the_catalog_fallback_resolves_windows_the_same_way(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from semantic_rails.planner import plan as plan_module
    from semantic_rails.planner.intent_ir import parse_intent
    from semantic_rails.planner.orchestrator import CompositionResult

    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, intent: CompositionResult(
            intent_ir=parse_intent(runtime, intent), draft=None
        ),
    )
    try:
        resolved = plan_payload(runtime, intent="orders in March 2017", detail="best")
        unclear = plan_payload(runtime, intent="orders since March 2017", detail="best")
    finally:
        runtime.close()
    assert resolved["best"]["pattern"] == "catalog_fallback"
    time = resolved["best"]["query_ir"]["time"]
    assert (time["start"], time["end"], time["grain"]) == ("2017-03-01", "2017-04-01", "month")
    assert unclear["status"] == "low_confidence"
    assert unclear["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
