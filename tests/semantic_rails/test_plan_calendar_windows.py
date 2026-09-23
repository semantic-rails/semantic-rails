"""plan resolves calendar years, half years and days, and buckets totals once.

A question scoped to "2017", "the first half of 2017" or "April 1 to April 7,
2017" used to lose its window silently, so the draft answered an unbounded
question with status ok. A total over a window also needs a grain that yields
a single bucket: without one, rows group by the raw timestamp.
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


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("total revenue in 2017", YEAR_2017),
        ("monthly revenue by store for 2017", YEAR_2017),
        ("2017 revenue by store", YEAR_2017),
        ("revenue in 2016 and 2017", {"start": "2016-01-01", "end": "2018-01-01"}),
        ("gross profit in the first half of 2017", {"start": "2017-01-01", "end": "2017-07-01"}),
        ("H2 2017 orders", {"start": "2017-07-01", "end": "2018-01-01"}),
        ("orders from April 1 to April 7, 2017", {"start": "2017-04-01", "end": "2017-04-08"}),
        ("orders April 1-7, 2017", {"start": "2017-04-01", "end": "2017-04-08"}),
        (
            "orders between March 30 and April 2, 2017",
            {"start": "2017-03-30", "end": "2017-04-03"},
        ),
        ("revenue on April 3, 2017", {"start": "2017-04-03", "end": "2017-04-04"}),
        # More specific forms still win over the year they contain.
        ("revenue by store in Q2 2017", {"start": "2017-04-01", "end": "2017-07-01"}),
        ("orders in March 2017", {"start": "2017-03-01", "end": "2017-04-01"}),
    ],
)
def test_calendar_windows_resolve(text: str, bounds: dict[str, str]) -> None:
    assert _time_bounds_from_text(text) == bounds
    assert _unresolved_time_phrases(text) == []


@pytest.mark.parametrize("text", ["top 2000 customers", "orders over 2000", "at least 2050 items"])
def test_quantities_are_not_years(text: str) -> None:
    assert _time_bounds_from_text(text) == {}
    assert _unresolved_time_phrases(text) == []


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        # A year-over-year comparison is not a one-year window.
        ("revenue 2017 vs 2016", "2017 vs 2016"),
        # An impossible date never widens to its year.
        ("revenue on Feb 30, 2017", "feb 30, 2017"),
        ("revenue since 2016", "since 2016"),
    ],
)
def test_unresolvable_windows_are_reported(text: str, phrase: str) -> None:
    assert _time_bounds_from_text(text) == {}
    assert _unresolved_time_phrases(text) == [phrase]


@pytest.mark.parametrize(
    ("text", "grain"),
    [
        ("total revenue in 2017", "year"),
        ("gross profit in the first half of 2017", "year"),
        ("item revenue from March through May 2017", "year"),
        ("orders in March 2017", "month"),
        ("revenue by store in Q2 2017", "quarter"),
        ("revenue on April 3, 2017", "day"),
        ("orders from April 1 to April 7, 2017", "month"),
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


def test_plan_flags_a_year_comparison_it_cannot_bound(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue 2017 vs 2016", detail="query")
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    finally:
        runtime.close()
