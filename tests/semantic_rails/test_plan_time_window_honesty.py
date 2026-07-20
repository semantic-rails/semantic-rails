"""Regression tests: plan must not silently drop time windows.

The honesty bug: an intent like "revenue by store last month" produced a
plan with NO time range and status ``ok`` — the agent then executed an
unbounded query believing it answered the time-scoped question.

Two-part contract under test:

1. Common relative windows ("last month", "last 7 days", "this year",
   "yesterday", ...) resolve into the Query IR's existing relative range
   form (``time.range.last``) or absolute ``start``/``end`` bounds.
2. Temporal phrases the planner detects but cannot resolve ("last few
   weeks", "since 2023") downgrade the plan to ``low_confidence`` with a
   structured ``why`` naming the unresolved phrase and recovery hints
   listing the supported window forms — instead of marking the unbounded
   draft ready to execute.
"""

from __future__ import annotations

from datetime import date

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import (
    _time_bounds_from_text,
    _time_spec,
    _unresolved_time_phrases,
)
from semantic_rails.planner.intent_ir import parse_intent

# ---------------------------------------------------------------------------
# Pure helper-level tests (no runtime needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "unit", "value"),
    [
        ("revenue by store last month", "month", 1),
        ("revenue by store last 7 days", "day", 7),
        ("orders last week", "week", 1),
        ("orders past 2 weeks", "week", 2),
        ("orders last three quarters", "quarter", 3),
        ("revenue trailing 12 months", "month", 12),
        ("revenue previous year", "year", 1),
        ("orders last quarter", "quarter", 1),
    ],
)
def test_relative_windows_resolve_to_range_last(text: str, unit: str, value: int) -> None:
    bounds = _time_bounds_from_text(text)
    assert bounds == {"range": {"last": {"unit": unit, "value": value}}}


def test_yesterday_resolves_to_one_day_relative_range() -> None:
    assert _time_bounds_from_text("orders yesterday") == {
        "range": {"last": {"unit": "day", "value": 1}}
    }


def test_this_year_resolves_to_current_calendar_year_bounds() -> None:
    bounds = _time_bounds_from_text("revenue this year")
    today = date.today()
    assert bounds == {
        "start": date(today.year, 1, 1).isoformat(),
        "end": date(today.year + 1, 1, 1).isoformat(),
    }


def test_quarter_year_bounds_still_resolve() -> None:
    # Pre-existing behavior that must survive the resolver rework.
    assert _time_bounds_from_text("revenue in Q2 2025") == {
        "start": "2025-04-01",
        "end": "2025-07-01",
    }


def test_this_month_bounds_still_resolve() -> None:
    bounds = _time_bounds_from_text("revenue this month")
    today = date.today()
    assert bounds["start"] == today.replace(day=1).isoformat()
    assert "end" in bounds


@pytest.mark.parametrize(
    "text",
    [
        # Comparison contexts belong to the period-shift patterns — the
        # resolver must NOT bound the query there.
        "revenue this month vs last month",
        "revenue compared to last year",
        "orders versus last week",
    ],
)
def test_comparison_contexts_do_not_resolve_to_a_window(text: str) -> None:
    bounds = _time_bounds_from_text(text)
    assert "range" not in bounds
    # ... and they are not flagged unresolved either: the period-shift
    # patterns own those phrasings.
    leftovers = [p for p in _unresolved_time_phrases(text) if "last" in p or "versus" in p]
    assert leftovers == []


@pytest.mark.parametrize(
    ("text", "expected_phrase"),
    [
        ("revenue by store over the last few weeks", "last few weeks"),
        ("orders in the past couple of months", "past couple of months"),
        ("revenue since 2023", "since 2023"),
        ("orders since January", "since january"),
        ("revenue last holiday season", "last holiday"),
    ],
)
def test_unresolvable_temporal_phrases_are_detected(text: str, expected_phrase: str) -> None:
    phrases = _unresolved_time_phrases(text)
    assert any(expected_phrase in phrase for phrase in phrases), (
        f"expected {expected_phrase!r} to be flagged, got {phrases!r}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "revenue by store last month",
        "revenue by store last 7 days",
        "orders yesterday",
        "revenue this year",
        "revenue by store",  # no temporal phrase at all
    ],
)
def test_resolved_or_absent_windows_are_not_flagged(text: str) -> None:
    assert _unresolved_time_phrases(text) == []


def test_time_spec_grain_follows_resolved_window_unit() -> None:
    spec = _time_spec("temporal_role.jaffle_order_time", "revenue last 7 days")
    assert spec["grain"] == "day"
    spec = _time_spec("temporal_role.jaffle_order_time", "orders yesterday")
    assert spec["grain"] == "day"
    spec = _time_spec("temporal_role.jaffle_order_time", "revenue by store last month")
    assert spec["grain"] == "month"
    # No window, no explicit grain cue — the month default stands.
    spec = _time_spec("temporal_role.jaffle_order_time", "revenue by store")
    assert spec["grain"] == "month"
    assert "range" not in spec


# ---------------------------------------------------------------------------
# Plan-surface tests (runtime needed)
# ---------------------------------------------------------------------------


def test_plan_last_month_is_time_bounded_and_ready(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue by store last month")
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    time_spec = payload["best"]["query_ir"]["time"]
    assert time_spec["range"] == {"last": {"unit": "month", "value": 1}}
    assert payload["best"]["validation_ok"] is True
    assert payload["next"]["ready_for"] == ["compile", "execute"]


def test_plan_last_n_days_is_time_bounded(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue by store last 7 days")
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    time_spec = payload["best"]["query_ir"]["time"]
    assert time_spec["range"] == {"last": {"unit": "day", "value": 7}}
    assert time_spec["grain"] == "day"


def test_plan_unresolved_window_downgrades_instead_of_silently_dropping(
    runtime_factory,
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue by store over the last few weeks")
    finally:
        runtime.close()
    assert payload["status"] == "low_confidence"
    why = payload["why"]
    assert why["code"] == "TIME_WINDOW_UNRESOLVED"
    assert "last few weeks" in why["details"]["unresolved_phrases"]
    assert why["details"]["path"] == "time"
    hints = why["recovery_hints"]
    assert hints, "downgrade must carry recovery hints"
    hint_text = " ".join(str(hint.get("message", "")) for hint in hints)
    # The hints must teach the supported window forms.
    assert "last N days" in hint_text
    assert "time.start" in hint_text or "start" in hint_text
    # The unbounded draft must NOT be flagged ready to execute.
    assert "ready_for" not in payload["next"]


def test_plan_since_year_downgrades(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue since 2023")
    finally:
        runtime.close()
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert "since 2023" in payload["why"]["details"]["unresolved_phrases"]


def test_partial_query_bounds_suppress_the_downgrade(runtime_factory) -> None:
    """When the caller supplies explicit bounds the window is not
    dropped — the plan must stay ``ok`` even though the phrase itself
    did not resolve."""

    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime,
            intent="revenue over the last few weeks",
            partial_query={"time": {"start": "2025-01-01", "end": "2025-02-01"}},
        )
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    time_spec = payload["best"]["query_ir"]["time"]
    assert time_spec["start"] == "2025-01-01"
    assert time_spec["end"] == "2025-02-01"
    assert payload["next"]["ready_for"] == ["compile", "execute"]


def test_plan_period_shift_comparison_is_not_window_bounded(runtime_factory) -> None:
    """ "vs last month" is a period-shift comparison, not a trailing
    window — the prior_period expression owns its own lookback."""

    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue this month vs last month")
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert payload["best"]["pattern"] == "inline_period_shift"
    time_spec = payload["best"]["query_ir"]["time"]
    assert "range" not in time_spec
    assert "start" not in time_spec


def test_intent_ir_time_carries_resolved_relative_range(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        intent_ir = parse_intent(runtime, "revenue by store last month")
    finally:
        runtime.close()
    assert intent_ir.time is not None
    assert intent_ir.time["range"] == {"last": {"unit": "month", "value": 1}}
    assert intent_ir.to_dict()["time"]["range"]["last"]["unit"] == "month"


def test_relevance_floor_still_blocks_nonsense_intents(runtime_factory) -> None:
    """The honesty gate must not weaken the relevance floor."""

    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="blue whale migration patterns")
    finally:
        runtime.close()
    assert payload["status"] == "out_of_scope"
    assert payload["best"] is None
