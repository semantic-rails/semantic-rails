"""A period comparison is ready only when every period it returns has ended at the request's now.

A comparison takes no window but an end: validation refuses one beside ``last``, ``this`` or a
``start``. The check reads the draft's engine form, so with no ``time.end``, or one after
``now`` or off a bucket boundary, a draft that compares periods is held with
``PERIOD_COMPARISON_INCOMPLETE``, and its option's call ends on the last complete period. Every
other draft answers as it does without the check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.period_completeness import compares_periods, incomplete_period_why
from semantic_rails.planner import examples as examples_module
from semantic_rails.planner import intent_holds
from semantic_rails.planner.plan import plan_payload
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from tests.semantic_rails.planner_fixtures import jaffle_runtime

HELD = "PERIOD_COMPARISON_INCOMPLETE"
METRIC = "metric.sales.month_over_month_revenue_growth"
ROLE = "temporal_role.jaffle_order_time"
REVENUE = "measure.jaffle.revenue_usd"
NOW = "2018-07-15T12:00:00Z"
PRIOR_MONTH = {"kind": "prior_period", "input": REVENUE, "offset": {"unit": "month", "value": 1}}
GRANT = RequestContext(
    actor="subject",
    roles=("analyst",),
    audience="finance",
    metric_allowlist=(METRIC,),
    dimension_allowlist=(ROLE,),
)

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def _ready(payload: dict[str, Any]) -> bool:
    return "execute" in (payload.get("next") or {}).get("ready_for", [])


def _assert_held(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["status"] == "low_confidence", payload.get("why")
    assert payload["why"]["code"] == HELD, payload.get("why")
    assert not _ready(payload)
    (hint,) = payload["why"]["recovery_hints"]
    assert hint["kind"] == "compare_complete_periods"
    assert hint["message"].endswith(("then plan again.", "or later."))
    return payload["why"]["details"]


def _metric(metric_id: str, expression: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=metric_id, expression=parse_semantic_expression(expression, context="config")
    )


# A growth metric dividing by its prior month, one built on it, and one comparing nothing.
_SUM = {"kind": "aggregate", "measure": "m", "aggregation": "sum"}
_CONFIG = SimpleNamespace(
    temporal_roles=[SimpleNamespace(id="role", timezone="UTC")],
    dimensions=[SimpleNamespace(id="dimension.ordered_on", data_type="date")],
    metric_recipes=[
        _metric(
            "metric.growth",
            {
                "kind": "arithmetic",
                "op": "divide",
                "left": _SUM,
                "right": {
                    "kind": "prior_period",
                    "input": _SUM,
                    "offset": {"unit": "month", "value": 1},
                },
            },
        ),
        _metric(
            "metric.growth_pct",
            {
                "kind": "arithmetic",
                "op": "multiply",
                "left": {"kind": "metric", "metric": "metric.growth"},
                "right": {"kind": "literal", "value": 100},
            },
        ),
        _metric("metric.total", {"kind": "aggregate", "measure": "m"}),
    ],
)
_COMPARING = [
    {"expression": {"kind": "prior_period", "input": _SUM, "offset": {"unit": "day", "value": 1}}},
    {"expression": {"kind": "metric", "metric": "metric.growth_pct"}},
]


def _engine(select: dict[str, Any], time: dict[str, Any], now: Any, **extra: Any) -> dict:
    return {"select": [select], "time": time, "policy_context": {"now": now}, **extra}


@pytest.mark.parametrize(
    ("grain", "end", "now", "complete_end"),
    [
        # No end: the window runs to now, so the period holding now is in progress.
        ("month", None, "2024-07-15T12:00:00Z", "2024-07-01"),
        ("year", None, "2024-01-01T00:00:00Z", "2024-01-01"),
        # A date is its midnight: that day has not ended.
        ("day", "2024-07-15", "2024-07-15", None),
        ("day", "2024-07-16", "2024-07-15", "2024-07-15"),
        ("day", "2024-07-15T12:00:00", "2024-07-16T00:00:00Z", "2024-07-15"),
        # Weeks start on Monday; 2024-07-14 is a Sunday.
        ("week", "2024-07-15", "2024-07-15T00:00:00Z", None),
        ("week", "2024-07-14", "2024-07-20T00:00:00Z", "2024-07-08"),
        ("month", "2024-07-01", "2024-07-31T23:59:59Z", None),
        ("month", "2024-08-01", "2024-07-31T23:59:59Z", "2024-07-01"),
        ("quarter", "2024-07-01", "2024-08-15T00:00:00Z", None),
        ("quarter", "2024-08-01", "2024-08-15T00:00:00Z", "2024-07-01"),
        ("year", "2024-01-01", "2024-07-15T12:00:00Z", None),
    ],
)
def test_a_period_is_complete_when_its_bucket_has_ended_by_now(
    grain: str, end: str | None, now: str, complete_end: str | None
) -> None:
    time = {"temporal_role": "role", "grain": grain, **({"end": end} if end else {})}
    for select in _COMPARING:
        why = incomplete_period_why(_CONFIG, _engine(select, time, now))
        if complete_end is None:
            assert why is None
        else:
            assert why is not None and why["code"] == HELD
            assert why["details"]["complete_end"] == complete_end


def test_the_clocks_zone_cuts_the_buckets() -> None:
    """2024-07-01T02:00Z is still June 30 in New York, so June hasn't ended there."""
    zoned = [SimpleNamespace(id="role", timezone="America/New_York")]
    config = SimpleNamespace(**{**vars(_CONFIG), "temporal_roles": zoned})
    time = {"temporal_role": "role", "grain": "month", "end": "2024-07-01"}
    why = incomplete_period_why(config, _engine(_COMPARING[1], time, "2024-07-01T02:00:00Z"))
    assert why is not None and why["details"]["incomplete_period"]["start"] == "2024-06-01"
    ended = _engine(_COMPARING[1], time, "2024-07-01T05:00:00Z")
    assert incomplete_period_why(config, ended) is None


@pytest.mark.parametrize(
    "select",
    [
        {"expression": {"kind": "measure", "measure": "m"}},
        {"expression": {"kind": "metric", "metric": "metric.total"}},
    ],
)
def test_a_draft_that_compares_nothing_is_not_checked(select: dict[str, Any]) -> None:
    time = {"temporal_role": "role", "grain": "month"}
    assert incomplete_period_why(_CONFIG, _engine(select, time, NOW)) is None


def test_a_clock_that_cannot_be_read_holds_a_comparison() -> None:
    time = {"temporal_role": "role", "grain": "month", "end": "2018-01-01"}
    why = incomplete_period_why(_CONFIG, _engine(_COMPARING[1], time, "next tuesday"))
    assert why is not None and why["code"] == HELD
    assert why["details"] == {"path": "now"}


@pytest.mark.parametrize(
    "query",
    [
        # Execution rejects each: a select that isn't a list, an unknown kind.
        {"select": {"expression": {"kind": "measure", "measure": "m"}}},
        {"select": [{"expression": {"kind": "prior_periods", "measure": "m"}}]},
        # A metric whose definition can't be read.
        {"select": [{"expression": {"kind": "metric", "metric": "metric.unknown"}}]},
    ],
)
def test_a_draft_the_check_cannot_read_counts_as_a_comparison(query: dict[str, Any]) -> None:
    assert compares_periods(_CONFIG, query)
    why = incomplete_period_why(_CONFIG, {**query, "policy_context": {"now": NOW}})
    assert why is not None and why["code"] == HELD


def test_a_dated_where_hold_never_asks_to_remove_the_filter() -> None:
    time = {"temporal_role": "role", "grain": "month", "end": "2018-01-01"}
    where = [{"field": "dimension.ordered_on", "op": "lt", "value": "2017-12-15"}]
    why = incomplete_period_why(_CONFIG, _engine(_COMPARING[1], time, NOW, where=where))
    assert why is not None and why["details"]["fields"] == ["dimension.ordered_on"]
    (hint,) = why["recovery_hints"]
    assert hint["message"] == (
        "Compare complete months through December 2017: set time.end to 2018-01-01 "
        "(end-exclusive), then plan again."
    )


@pytest.mark.parametrize("end", [None, "2018-01-01"])
def test_a_metric_that_compares_periods_is_checked_on_every_plan_path(
    runtime: Runtime, end: str | None
) -> None:
    """The governed growth metric divides by its prior month: plan and the granted-metric
    shortcut both hold it until its window ends on a month that has ended."""

    time = {"temporal_role": ROLE, "grain": "month", **({"end": end} if end else {})}
    planned = plan_payload(
        runtime,
        intent="month over month revenue growth by month",
        partial_query={"time": {"end": end}} if end else None,
        now=NOW,
    )
    granted = plan_payload(
        runtime,
        intent="month-over-month revenue growth",
        partial_query={"policy_context": GRANT.to_policy_context(), "time": time},
        now=NOW,
    )
    assert granted["best"]["pattern"] == "granted_metric"
    for payload in (planned, granted):
        assert METRIC in str(payload["best"]["query"]["select"])
        if end:
            assert payload["status"] == "ok" and _ready(payload), payload.get("why")
        else:
            details = _assert_held(payload)
            assert details["complete_end"] == details["incomplete_period"]["start"]


def test_the_option_plans_ready_and_returns_only_ended_months(runtime: Runtime) -> None:
    intent = "month over month revenue growth by month"
    held = plan_payload(runtime, intent=intent, now=NOW)
    details = _assert_held(held)
    assert details["incomplete_period"] == {"start": "2018-07-01", "end": "2018-08-01"}
    (option,) = details["options"]
    assert option["reads"] == "every month through June 2018"
    assert option["call"] == {
        **held["best"]["query"],
        "time": {**held["best"]["query"]["time"], "end": "2018-07-01"},
    }
    again = plan_payload(
        runtime, intent=intent, partial_query={"time": {"end": "2018-07-01"}}, now=NOW
    )
    assert again["status"] == "ok" and _ready(again), again.get("why")
    rows = runtime.query(option["call"], now=NOW)["rows"]
    assert rows and all(not row["period_partial"] for row in rows)
    assert all(row["period"] < "2018-07-01" for row in rows)


@pytest.fixture
def example_runtime(runtime: Runtime) -> Iterator[Callable[[str | None], Runtime]]:
    saved = runtime._package_examples

    def _with_example(end: str | None) -> Runtime:
        time = {"temporal_role": ROLE, "grain": "month", **({"end": end} if end else {})}
        example = {
            "question": "Revenue growth on the month before",
            "query": {"select": [METRIC], "time": time},
        }
        runtime._package_examples = [("growth", example)]
        return runtime

    yield _with_example
    runtime._package_examples = saved


@pytest.mark.parametrize("end", [None, "2018-07-01"])
def test_an_authored_example_goes_through_the_same_check(
    example_runtime: Callable[[str | None], Runtime],
    monkeypatch: pytest.MonkeyPatch,
    end: str | None,
) -> None:
    """A package example's authored call answers its question only over ended periods."""

    runtime = example_runtime(end)
    question = "Revenue growth on the month before"
    payload = plan_payload(runtime, intent=question, now=NOW)
    assert payload["best"]["pattern"] == "package_example"
    assert payload["best"]["validation_ok"] is True
    if end:
        assert payload["status"] == "ok" and _ready(payload), payload.get("why")
        return
    details = _assert_held(payload)
    assert details["incomplete_period"] == {"start": "2018-07-01", "end": "2018-08-01"}
    # Nothing else holds it: without the check, the example was ready to execute.
    monkeypatch.setattr(examples_module, "_incomplete_period_why", lambda *args, **kwargs: None)
    assert _ready(plan_payload(runtime, intent=question, now=NOW))


def test_a_caller_select_that_compares_periods_is_held(runtime: Runtime) -> None:
    payload = plan_payload(
        runtime,
        intent="revenue by month",
        partial_query={
            "select": [REVENUE, {"expression": PRIOR_MONTH, "as": "month_before"}],
            "time": {"temporal_role": ROLE, "grain": "month"},
        },
        now=NOW,
    )
    assert "month_before" in str(payload["best"]["query"]["select"])
    _assert_held(payload)


@pytest.mark.parametrize(
    "intent", ["revenue by month", "orders by store last month", "revenue month over month"]
)
def test_a_draft_that_compares_nothing_plans_as_before(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    checked = plan_payload(runtime, intent=intent, now=NOW)
    monkeypatch.setattr(intent_holds, "incomplete_period_why", lambda *args, **kwargs: None)
    unchecked = plan_payload(runtime, intent=intent, now=NOW)
    for key in ("status", "why", "next", "best", "warnings"):
        assert checked.get(key) == unchecked.get(key), key
