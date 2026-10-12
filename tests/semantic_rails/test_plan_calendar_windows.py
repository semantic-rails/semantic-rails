"""plan resolves clear calendar windows, reports the rest, and buckets totals once.

A question scoped to "in 2017", "the first half of 2017" or "April 1 to April
7, 2017" used to lose its window silently, so the draft answered an unbounded
question with status ok. The planner now resolves a window only when the
question names exactly one, in a form it reads unambiguously. A bound ("before
2017", "since March 2017"), a qualifier ("early 2017", "the end of 2017"), a
comparison year, a numeric date or two windows at once is reported as
unresolved instead of being narrowed or widened to the nearest form that
parses. A total over a window is one total: the window asks for no grain.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.call import Call
from semantic_rails.planner.groupings import _time_spec
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.planner.patterns.metric_by_dimension_rollup import _unresolved_time_phrases
from semantic_rails.planner.time_windows import _time_bounds_from_text, _time_window
from semantic_rails.runtime import Runtime
from tests.semantic_rails.planner_fixtures import jaffle_runtime

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
        ("orders between April 3 and April 5, 2017", {"start": "2017-04-03", "end": "2017-04-06"}),
        ("revenue between March and May 2017", {"start": "2017-03-01", "end": "2017-06-01"}),
        ("orders over 2000 in 2017", YEAR_2017),
        # "since" a named month starts there and stays open.
        ("orders since March 2017", {"start": "2017-03-01"}),
    ],
)
def test_clear_calendar_windows_resolve(text: str, bounds: dict[str, str]) -> None:
    assert _time_bounds_from_text(text) == bounds
    assert _unresolved_time_phrases(text) == []


MULTIPLE_RELATIVE_WINDOWS = [
    "revenue last month and this month",
    "revenue this month and last month",
    "revenue this month and this year",
    "revenue this year and this month",
    "revenue current month and last month",
    "revenue last month and current month",
]


@pytest.mark.parametrize("text", MULTIPLE_RELATIVE_WINDOWS)
def test_multiple_relative_windows_are_unresolved(runtime: Runtime, text: str) -> None:
    assert _time_bounds_from_text(text) == {}
    phrases = _unresolved_time_phrases(text)
    assert len(phrases) == 2, phrases
    payload = SemanticLayerMCPAdapter(runtime).call_tool("plan", {"intent": text})
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert len(payload["why"]["details"]["unresolved_phrases"]) == 2
    assert not {"start", "end", "last", "this"} & set(_best(payload).get("time") or {})


@pytest.mark.parametrize(
    "text",
    [
        "revenue last month",
        "revenue this month",
        "revenue current year",
        # A comparison's relative period stays outside window resolution.
        "revenue this month vs last month",
        "revenue this month compared to last month",
    ],
)
def test_single_relative_window_still_resolves(text: str) -> None:
    assert _time_bounds_from_text(text)
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
        # "and" names two periods; only "between ... and ..." is a range.
        ("revenue in March and May 2017", "march and may 2017"),
        ("revenue in January and December 2017", "january and december 2017"),
        ("orders on April 3 and April 5, 2017", "april 3 and april 5, 2017"),
        ("revenue on April 3 and 10, 2017", "april 3 and 10, 2017"),
        ("orders on March 30 and April 2, 2017", "march 30 and april 2, 2017"),
        ("revenue for the 1st and 15th of April 2017", "15th of april 2017"),
        # Two years compared, even where "over 2000" alone would be a count.
        ("revenue in 2017 over 2016", "2017 over 2016"),
        ("revenue in 2017 below 2016", "2017 below 2016"),
        ("did we make more revenue in 2017 than in 2016", "2017 than in 2016"),
        # A month after its year, and a fiscal year.
        ("revenue in 2017 March", "in 2017"),
        ("revenue in 2017/18", "in 2017"),
    ],
)
def test_unclear_windows_are_reported(text: str, phrase: str) -> None:
    assert _time_bounds_from_text(text) == {}
    phrases = _unresolved_time_phrases(text)
    assert any(phrase in reported for reported in phrases), phrases


@pytest.mark.parametrize(
    ("text", "grain"),
    [
        # A window with no grain asked for is one total.
        ("total revenue in 2017", ""),
        ("gross profit in the first half of 2017", ""),
        ("item revenue from March through May 2017", ""),
        ("orders in March 2017", ""),
        ("revenue by store in Q2 2017", ""),
        ("revenue on April 3, 2017", ""),
        ("orders from April 1 to April 7, 2017", ""),
        ("orders from July 3 to July 9, 2017", ""),
        ("revenue in 2016 and 2017", ""),
        # An explicit grain or a trend cue sets one.
        ("monthly revenue by store for 2017", "month"),
        ("daily orders from April 1 to April 7, 2017", "day"),
        ("revenue trend in 2017", "month"),
        ("revenue by month", "month"),
        # An explicit grain beats the quarter the window names.
        ("monthly revenue in Q2 2017", "month"),
        ("weekly orders in Q2 2017", "week"),
        ("revenue per day in Q2 2017", "day"),
    ],
)
def test_totals_over_a_window_get_one_bucket(text: str, grain: str) -> None:
    assert _time_spec("temporal_role.jaffle_order_time", text).grain == grain


ORDER_TIME = "temporal_role.jaffle_order_time"
LONG = "revenue " + "please " * 300


runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def _best(payload: dict[str, Any]) -> dict[str, Any]:
    return (payload.get("best") or {}).get("query") or {}


def _without_primary(monkeypatch: pytest.MonkeyPatch, draft: Any = None) -> None:
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, intent: CompositionResult(
            intent_ir=parse_intent(runtime, intent), draft=draft, pattern="test" if draft else ""
        ),
    )


@pytest.mark.parametrize(
    ("intent", "partial", "status", "code", "time"),
    [
        ("total revenue in 2017", None, "ok", None, {"temporal_role": ORDER_TIME, **YEAR_2017}),
        (
            "monthly revenue by store for 2017",
            None,
            "ok",
            None,
            {"temporal_role": ORDER_TIME, "grain": "month", **YEAR_2017},
        ),
        # "since" a named month starts there and stays open.
        (
            "orders since March 2017",
            None,
            "ok",
            None,
            {"temporal_role": ORDER_TIME, "start": "2017-03-01"},
        ),
        ("revenue 2017 vs 2016", None, "low_confidence", "TIME_WINDOW_UNRESOLVED", None),
        ("revenue before 2017", None, "low_confidence", "TIME_WINDOW_UNRESOLVED", None),
        (
            LONG + "in 2017",
            {"time": {"temporal_role": ORDER_TIME, "grain": "year", **YEAR_2017}},
            "ok",
            None,
            YEAR_2017,
        ),
    ],
)
def test_plan_keeps_the_window_it_resolves_and_reports_the_rest(
    runtime: Runtime, intent: str, partial: Any, status: str, code: str | None, time: Any
) -> None:
    payload = plan_payload(runtime, intent=intent, partial_query=partial, detail="query")
    assert payload["status"] == status, payload.get("why")
    assert (payload.get("why") or {}).get("code") == code
    drafted = _best(payload).get("time") or {}
    if time is None:
        assert not {"start", "end"} & set(drafted)
    else:
        # A caller's window comes back with its own role and grain.
        assert (drafted if partial is None else {key: drafted[key] for key in time}) == time


@pytest.mark.parametrize(
    ("intent", "window", "code"),
    [
        (
            "orders since March 2017",
            {"start": "2017-03-01", "end": "2018-01-01"},
            "TIME_WINDOW_UNRESOLVED",
        ),
        (
            "month-over-month revenue growth by month in 2017",
            YEAR_2017,
            "TIME_WINDOW_START_DROPPED",
        ),
    ],
)
def test_a_callers_explicit_window_settles_the_question_and_keeps_its_start(
    runtime: Runtime, intent: str, window: dict[str, str], code: str
) -> None:
    payload = plan_payload(runtime, intent=intent, partial_query={"time": window}, detail="query")
    assert (payload.get("why") or {}).get("code") != code
    assert _best(payload)["time"]["start"] == window["start"]


def test_a_lookback_metric_keeps_its_end_and_says_so(runtime: Runtime) -> None:
    payload = plan_payload(
        runtime, intent="month-over-month revenue growth by month in 2017", detail="query"
    )
    time = _best(payload)["time"]
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "TIME_WINDOW_START_DROPPED"
    assert payload["why"]["details"]["requested_start"] == "2017-01-01"
    assert "start" not in time and time["end"] == "2018-01-01"
    # The draft runs: the engine can't bound this metric's start, but it can its end.
    checked = SemanticLayerMCPAdapter(runtime).call_tool(
        "execute", {"mode": "validate", "query": _best(payload)}
    )
    assert checked["ok"] is True


def test_the_catalog_fallback_resolves_windows_the_same_way(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_primary(monkeypatch)
    resolved = plan_payload(runtime, intent="orders in March 2017", detail="best")
    since = plan_payload(runtime, intent="orders since March 2017", detail="best")
    assert resolved["best"]["pattern"] == "catalog_fallback"
    time = resolved["best"]["query"]["time"]
    assert (time["start"], time["end"]) == ("2017-03-01", "2017-04-01")
    assert "grain" not in time
    # "since" a named month starts there and stays open, through the fallback too.
    assert since["status"] == "ok", since.get("why")
    assert since["best"]["pattern"] == "catalog_fallback"
    assert since["best"]["query"]["time"]["start"] == "2017-03-01"
    assert "end" not in since["best"]["query"]["time"]


def test_today_follows_the_date(monkeypatch: pytest.MonkeyPatch) -> None:
    # "today" and "this month" are relative windows: the call carries no dates for them.
    assert _time_bounds_from_text("orders today") == {"this": "day"}
    assert _time_bounds_from_text("revenue this month") == {"this": "month"}
    # A named month is the latest one already begun, so its year follows the date.
    before = _time_bounds_from_text("revenue in March")
    monkeypatch.setattr(
        "semantic_rails.clock.current_time", lambda: datetime(2001, 2, 3, tzinfo=UTC)
    )
    march = {"start": "2000-03-01", "end": "2000-04-01"}
    assert _time_bounds_from_text("revenue in March") == march
    assert before != march


def test_a_draft_cannot_edit_the_cached_window() -> None:
    first = _time_bounds_from_text("revenue in the last 3 months")
    first["last"]["value"] = 99
    assert _time_bounds_from_text("revenue in the last 3 months")["last"]["value"] == 3


def test_a_long_question_is_not_partially_resolved() -> None:
    started = time.perf_counter()
    assert _time_bounds_from_text("revenue " * 20000 + "in 2017") == {}
    assert _time_bounds_from_text("revenue in 2017 " + "please " * 300 + "but not March") == {}
    assert time.perf_counter() - started < 2


def test_time_resolution_accepts_a_question_at_the_length_limit() -> None:
    intent = "revenue in 2017".ljust(2000)
    assert _time_bounds_from_text(intent) == YEAR_2017
    assert _unresolved_time_phrases(intent) == []


@pytest.mark.parametrize(
    "intent", [LONG + "in 2017", "revenue in 2017 " + "please " * 300 + "and 2019"],
    ids=["trailing-window", "trailing-conflict"],
)  # fmt: skip
def test_plan_reports_an_incomplete_long_question(runtime: Runtime, intent: str) -> None:
    payload = SemanticLayerMCPAdapter(runtime).call_tool(
        "plan", {"intent": intent, "detail": "best"}
    )
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert payload["why"]["details"]["max_intent_chars"] == 2000
    assert payload["why"]["recovery_hints"][0]["kind"] == "rephrase_time_window"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert not {"start", "end"} & set(_best(payload).get("time") or {})


@pytest.mark.parametrize(
    ("detail", "bounds", "suffix", "settled"),
    [
        ("query", {"start": YEAR_2017["start"]}, "in 2017", False),
        ("best", {"end": YEAR_2017["end"]}, "in 2017", False),
        ("full", {"start": YEAR_2017["start"]}, "in 2017", False),
        ("query", YEAR_2017, "in 2017", True),
        # A question that states no year has none to disagree with a relative window.
        ("best", {"last": {"unit": "year", "value": 1}}, "for the last year", True),
        ("full", YEAR_2017, "in 2017", True),
    ],
)
def test_only_complete_bounds_in_the_query_argument_settle_a_long_question(
    runtime: Runtime, detail: str, bounds: dict[str, Any], suffix: str, settled: bool
) -> None:
    payload = SemanticLayerMCPAdapter(runtime).call_tool(
        "plan",
        {
            "intent": LONG + suffix,
            "detail": detail,
            "query": {"time": {"temporal_role": ORDER_TIME, "grain": "year", **bounds}},
        },
    )
    if settled:
        assert payload["status"] == "ok"
        assert all(_best(payload)["time"][key] == value for key, value in bounds.items())
        assert not payload.get("warnings")
        return
    # One endpoint does not settle it.
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert [gap["kind"] for gap in payload["why"]["details"]["gaps"]] == [
        "time_window_unrealized",
        "time_window_unresolved",
    ]
    hint = next(
        hint["message"]
        for hint in payload["why"]["recovery_hints"]
        if hint["kind"] == "provide_explicit_bounds"
    )
    # The hint names the tool's own argument.
    assert "query.time" in hint and "partial_query" not in hint


def test_a_period_comparison_offers_the_years_compared_as_one_series(runtime: Runtime) -> None:
    payload = plan_payload(
        runtime, intent="revenue by month in 2017 compared to last year", detail="query"
    )
    assert payload["status"] == "needs_clarification"
    assert payload["why"]["code"] == "PERIOD_COMPARISON"
    # The stated year, widened by the year before it, by month.
    series = {
        "temporal_role": ORDER_TIME,
        "grain": "month",
        "start": "2016-01-01",
        "end": "2018-01-01",
    }
    [option] = payload["why"]["details"]["options"]
    assert option["call"]["time"] == series
    assert _best(payload)["time"] == series


def test_a_drafts_other_window_does_not_answer_a_since_phrase(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A draft that carries a window of its own doesn't answer "since March 2017".
    bounded = Call.from_call(
        {
            "select": ["measure.jaffle.order_count"],
            "time": {"temporal_role": ORDER_TIME, "grain": "year", **YEAR_2017},
        }
    )
    _without_primary(
        monkeypatch,
        RuntimeCompositionDraft(query=bounded, resolved=[], rationale=[], interpreted_intent={}),
    )
    payload = plan_payload(runtime, intent="orders since March 2017", detail="query")
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert [gap["kind"] for gap in payload["why"]["details"]["gaps"]] == ["time_window_unrealized"]
    assert "execute" not in (payload.get("next") or {}).get("ready_for", [])


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("revenue for Q2", {"start": "2026-04-01", "end": "2026-07-01"}),
        ("revenue in March", {"start": "2026-03-01", "end": "2026-04-01"}),
    ],
)
def test_named_calendar_forms_resolve_on_a_fixed_clock(text, bounds):
    read = _time_window(text, {"now": "2026-10-05T06:00:00Z"})
    assert read.bounds == bounds
    assert not read.unresolved
