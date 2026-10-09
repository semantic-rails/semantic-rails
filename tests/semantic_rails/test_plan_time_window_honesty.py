"""Regression tests: plan must not silently drop time windows.

The honesty bug: an intent like "revenue by store last month" produced a
plan with NO time range and status ``ok`` — the agent then executed an
unbounded query believing it answered the time-scoped question.

Two-part contract under test:

1. Common relative windows ("last month", "last 7 days", "this year",
   "yesterday", ...) resolve into the call's relative window (``time.last``
   or ``time.this``); calendar windows resolve to ``start``/``end`` bounds.
2. Temporal phrases the planner detects but cannot resolve ("last few
   weeks", "since 2023") downgrade the plan to ``low_confidence`` with a
   structured ``why`` naming the unresolved phrase and recovery hints
   listing the supported window forms — instead of marking the unbounded
   draft ready to execute.
"""

from __future__ import annotations

import duckdb
import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan_payload
from semantic_rails.planner.groupings import _time_spec
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.patterns.metric_by_dimension_rollup import _unresolved_time_phrases
from semantic_rails.planner.time_windows import _time_bounds_from_text

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
def test_relative_windows_resolve_to_last(text: str, unit: str, value: int) -> None:
    bounds = _time_bounds_from_text(text)
    assert bounds == {"last": {"unit": unit, "value": value}}


def test_yesterday_resolves_to_the_last_day() -> None:
    assert _time_bounds_from_text("orders yesterday") == {"last": {"unit": "day", "value": 1}}


def test_this_year_resolves_to_this_year() -> None:
    assert _time_bounds_from_text("revenue this year") == {"this": "year"}


def test_quarter_year_bounds_still_resolve() -> None:
    # Pre-existing behavior that must survive the resolver rework.
    assert _time_bounds_from_text("revenue in Q2 2025") == {
        "start": "2025-04-01",
        "end": "2025-07-01",
    }


def test_this_month_still_resolves() -> None:
    assert _time_bounds_from_text("revenue this month") == {"this": "month"}


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
    assert "last" not in bounds
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


def test_time_spec_window_without_a_grain_is_one_total() -> None:
    spec = _time_spec("temporal_role.jaffle_order_time", "revenue last 7 days")
    assert (spec.grain, spec.last) == ("", ("day", 7))
    spec = _time_spec("temporal_role.jaffle_order_time", "orders yesterday")
    assert (spec.grain, spec.last) == ("", ("day", 1))
    spec = _time_spec("temporal_role.jaffle_order_time", "revenue by store last month")
    assert (spec.grain, spec.last) == ("", ("month", 1))
    # No window and no grain cue: no time block at all.
    assert _time_spec("temporal_role.jaffle_order_time", "revenue by store") is None


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
    time_spec = payload["best"]["query"]["time"]
    assert time_spec["last"] == {"unit": "month", "value": 1}
    assert payload["best"]["validation_ok"] is True
    assert payload["next"]["ready_for"] == ["execute"]


def test_plan_last_n_days_is_time_bounded(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue by store last 7 days")
    finally:
        runtime.close()
    time_spec = payload["best"]["query"]["time"]
    assert time_spec["last"] == {"unit": "day", "value": 7}
    # The question asks for no grain: the 7 days are one total per store, ready to run.
    assert "grain" not in time_spec
    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]


ONE_TOTAL_NOW = "2017-03-15T12:00:00Z"
REVENUE_BY_STORE = (
    "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0) "
    "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
    "WHERE o.ordered_at >= TIMESTAMP '{start}' AND o.ordered_at < TIMESTAMP '{end}' "
    "GROUP BY 1, 2 ORDER BY 1"
)


@pytest.mark.parametrize(
    ("intent", "reference"),
    [
        # At 2017-03-15 (UTC, the package's zone) the last 7 days are March 8 to 14...
        (
            "revenue by store last 7 days",
            REVENUE_BY_STORE.format(start="2017-03-08", end="2017-03-15"),
        ),
        # ...last month is February...
        (
            "revenue by store last month",
            REVENUE_BY_STORE.format(start="2017-02-01", end="2017-03-01"),
        ),
        # ...and yesterday is March 14.
        (
            "orders yesterday",
            "SELECT COUNT(DISTINCT order_id) FROM jaffle_order "
            "WHERE ordered_at >= TIMESTAMP '2017-03-14' AND ordered_at < TIMESTAMP '2017-03-15'",
        ),
    ],
)
def test_a_relative_window_without_a_grain_answers_one_total(
    runtime_factory, intent: str, reference: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent, now=ONE_TOTAL_NOW)
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query"]
        assert "last" in query["time"] and "grain" not in query["time"]
        rows = runtime.query(query, now=ONE_TOTAL_NOW)["rows"]
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            gold = connection.execute(reference).fetchall()
    finally:
        runtime.close()
    assert gold and all(row[-1] for row in gold)

    def rounded(values) -> tuple:
        return tuple(round(value, 2) if isinstance(value, float) else value for value in values)

    # One row per group, with no period column: the window is one total.
    assert all("period" not in row for row in rows)
    assert [rounded(row.values()) for row in rows] == [rounded(row) for row in gold]


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
    # There is no unbounded draft to execute at all.
    assert "ready_for" not in payload["next"]
    assert "query" not in payload["best"] and "validate" not in payload["next"]


@pytest.mark.parametrize("detail", ["best", "full", None])
def test_mcp_plan_offers_no_runnable_draft_without_the_window(
    runtime_factory, detail: str | None
) -> None:
    intent = "Revenue by store and month for January, February and March 2017"
    time = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}
    window = {**time, "start": "2017-01-01", "end": "2017-04-01"}
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        plan = mcp.call_tool("plan", {"intent": intent, **({"detail": detail} if detail else {})})
        bounded = mcp.call_tool("plan", {"intent": intent, "query": {"time": window}})
        long_plan = mcp.call_tool("plan", {"intent": "revenue by store " + "x " * 1000})
    finally:
        mcp.close()
    for refused in (plan, long_plan):
        assert refused["status"] == "low_confidence"
        assert refused["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
        rows = [refused["best"], *refused.get("alternatives", []), *refused.get("blocked", [])]
        assert all("query" not in row for row in rows)
        assert "validate" not in refused.get("next", {})
    # The recovery hint's way out: pass the window with its temporal role and grain.
    assert bounded["status"] == "ok"
    assert bounded["best"]["query"]["time"] == window


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
    time_spec = payload["best"]["query"]["time"]
    assert time_spec["start"] == "2025-01-01"
    assert time_spec["end"] == "2025-02-01"
    assert payload["next"]["ready_for"] == ["execute"]


def test_plan_period_shift_comparison_with_this_month_is_held(runtime_factory) -> None:
    """ "this month vs last month" compares a month not over yet with a complete
    one: held, with the two complete months offered as one series."""

    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue this month vs last month")
    finally:
        runtime.close()
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PERIOD_COMPARISON_INCOMPLETE"
    assert payload["why"]["details"]["window"] == {"this": "month"}
    assert payload["best"]["pattern"] == "inline_period_shift"
    assert "ready_for" not in payload["next"]
    time_spec = payload["best"]["query"]["time"]
    assert (time_spec["grain"], time_spec["last"]) == ("month", {"unit": "month", "value": 2})


def test_intent_ir_time_carries_the_relative_window(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        intent_ir = parse_intent(runtime, "revenue by store last month")
    finally:
        runtime.close()
    assert intent_ir.time is not None
    assert intent_ir.time["last"] == {"unit": "month", "value": 1}
    assert intent_ir.to_dict()["time"]["last"]["unit"] == "month"


def test_since_named_month_has_an_explicit_start():
    from semantic_rails.planner.time_windows import _time_window

    read = _time_window("orders since January", {"now": "2026-10-05T06:00:00Z"})
    assert read.bounds == {"start": "2026-01-01"}
    assert not read.unresolved
