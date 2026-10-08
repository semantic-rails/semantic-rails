"""A period comparison is ready only when every period it returns has ended at the request's now.

The real planner runs on the shop fixture with a frozen ``now`` and, as a live warehouse holds
it, no order at or after ``now``. A draft that compares periods and reaches a period still in
progress is held with ``PERIOD_COMPARISON_INCOMPLETE``, and its complete-periods alternative
returns only ended periods, each matching reference SQL. Every other case answers exactly as
it does without the check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.period_completeness import incomplete_period_why
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner.orchestrator import compose
from semantic_rails.planner.plan import plan_payload
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime

HELD = "PERIOD_COMPARISON_INCOMPLETE"
ROLE = "temporal_role.shop_order_ordered_at"
REVENUE = "measure.shop.revenue"
PRIOR_MONTH = {"kind": "prior_period", "measure": REVENUE, "offset": -1, "grain": "month"}
NOW = "2024-07-15T12:00:00Z"

# For each grain: mid-period, exactly at a period start, and one second before a period end.
CLOCKS = {
    "day": {
        "mid": "2024-07-15T12:00:00Z",
        "start": "2024-07-15T00:00:00Z",
        "end": "2024-07-15T23:59:59Z",
    },
    "week": {
        "mid": "2024-07-17T12:00:00Z",
        "start": "2024-07-15T00:00:00Z",
        "end": "2024-07-21T23:59:59Z",
    },
    "month": {
        "mid": "2024-07-15T12:00:00Z",
        "start": "2024-07-01T00:00:00Z",
        "end": "2024-07-31T23:59:59Z",
    },
    "quarter": {
        "mid": "2024-08-15T12:00:00Z",
        "start": "2024-07-01T00:00:00Z",
        "end": "2024-09-30T23:59:59Z",
    },
    "year": {
        "mid": "2024-07-15T12:00:00Z",
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-12-31T23:59:59Z",
    },
}
PHRASINGS = {
    "over": "revenue {unit} over {unit}",
    "vs-last": "revenue vs last {unit}",
    "this-vs-last": "revenue this {unit} vs last {unit}",
    "to-date": "revenue {unit} to date vs last {unit}",
    "previous": "revenue compared to the previous {unit}",
}
# No window, one that ended before every clock, and one holding the clock.
WINDOWS = {
    "none": dict.fromkeys(CLOCKS, ""),
    "past": {
        "day": " in May 2024",
        "week": " in May 2024",
        "month": " in May 2024",
        "quarter": " in 2023",
        "year": " in 2023",
    },
    "current": {
        "day": " in July 2024",
        "week": " in July 2024",
        "month": " in July 2024",
        "quarter": " in Q3 2024",
        "year": " in 2024",
    },
}
MATRIX = [
    pytest.param(grain, clock, phrasing, window, id=f"{grain}-{clock}-{phrasing}-{window}")
    for grain in CLOCKS
    for clock in CLOCKS[grain]
    for phrasing in PHRASINGS
    for window in WINDOWS
]

_AGGREGATES = {REVENUE: "SUM(amount)", "measure.shop.order_count": "COUNT(DISTINCT order_id)"}
_WIDTHS = {
    "day": "INTERVAL 1 DAY",
    "week": "INTERVAL 7 DAY",
    "month": "INTERVAL 1 MONTH",
    "quarter": "INTERVAL 3 MONTH",
    "year": "INTERVAL 1 YEAR",
}


def _utc(now: str) -> datetime:
    """``now`` as the naive UTC timestamp the orders table stores."""
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


# Every (package variant, now) a test runs on: the matrix clocks, and the boundary and zone cases.
LIVE = [
    *(
        ("utc_authored", now)
        for now in sorted(
            {now for clocks in CLOCKS.values() for now in clocks.values()}
            | {"2024-06-30T23:59:59Z", "2024-07-01", "2024-07-01T02:00:00Z"}
        )
    ),
    ("ny_authored", "2024-07-01T02:00:00Z"),
]


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[..., Runtime]]:
    """The shop runtime for a clock, holding no order at or after ``now``. Each connection
    opens here, as tests share it."""

    from tests.integration.correctness.conftest import _runtime, _write_variant

    runtimes: dict[tuple[str, str], Runtime] = {}
    try:
        for variant, now in LIVE:
            package = _write_variant(tmp_path_factory.mktemp("shop"), variant)
            seeding = _runtime(package)
            seeding.query({"version": 1, "select": [{"expression": {"measure": REVENUE}}]})
            seeding.close()
            with duckdb.connect(str(package / "data" / "warehouse.duckdb")) as connection:
                connection.execute("DELETE FROM orders WHERE ordered_at >= ?", [_utc(now)])
            runtime = runtimes[(variant, now)] = _runtime(package)
            runtime.query({"version": 1, "select": [{"expression": {"measure": REVENUE}}]})
        yield lambda now, variant="utc_authored": runtimes[(variant, now)]
    finally:
        for runtime in runtimes.values():
            runtime.close()


def _sql(runtime: Runtime, sql: str, params: list[Any]) -> tuple[Any, ...]:
    connection = runtime._get_adapter()._db.conn  # noqa: SLF001 - the reference shares the data
    return tuple(connection.execute(sql, params).fetchone())


def _unchecked(
    monkeypatch: pytest.MonkeyPatch, runtime: Any, intent: str, partial: dict[str, Any]
) -> dict[str, Any]:
    """The plan without the check: what plan answered before it."""
    with monkeypatch.context() as patch:
        patch.setattr(plan_module, "incomplete_period_why", lambda *args, **kwargs: None)
        return plan_payload(runtime, intent=intent, partial_query=partial)


def _ready(payload: dict[str, Any]) -> bool:
    return "execute" in payload["next"].get("ready_for", [])


def _assert_held(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["status"] == "low_confidence", payload.get("why")
    assert not _ready(payload)
    assert payload["why"]["code"] == HELD, payload["why"]
    # The only way forward offered is the complete-periods alternative, never "execute".
    assert [hint["kind"] for hint in payload["why"]["recovery_hints"]] == [
        "compare_complete_periods"
    ]
    return dict(payload["why"]["details"])


def _compares(query: dict[str, Any]) -> bool:
    """The draft carries a prior-period select (no shop metric defines one)."""

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            return node.get("kind") == "prior_period" or any(walk(v) for v in node.values())
        return isinstance(node, list) and any(walk(item) for item in node)

    return walk(query.get("select") or [])


def _reaches_incomplete(runtime: Runtime, time: dict[str, Any], now: str) -> bool:
    """The window's last bucket hasn't ended at now, or the window cuts it short (UTC)."""
    if not time.get("end"):
        return True
    aligned, ended = _sql(
        runtime,
        f"SELECT date_trunc('{time['grain']}', CAST(? AS TIMESTAMP)) = CAST(? AS TIMESTAMP), "
        "CAST(? AS TIMESTAMP) <= CAST(? AS TIMESTAMP)",
        [time["end"], time["end"], time["end"], _utc(now)],
    )
    return not (aligned and ended)


def _number(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _complete_rows(runtime: Runtime, query: dict[str, Any], now: str) -> list[tuple[Any, ...]]:
    """Run a two-select comparison: every bucket has ended at now, and each value and its
    prior-period value match reference SQL over the bucket and that bucket one shift earlier.
    Returns (bucket, value, prior value) rows."""

    rows = runtime.query({**query, "policy_context": {"now": now}})["rows"]
    grain = query["time"]["grain"]
    current, prior = query["select"]
    aggregate = _AGGREGATES[current["expression"]["measure"]]
    width, back = _WIDTHS[grain], _WIDTHS[prior["expression"]["grain"]]
    reference = (
        f"SELECT CAST(? AS TIMESTAMP) + {width} <= CAST(? AS TIMESTAMP), "
        f"(SELECT {aggregate} FROM orders WHERE ordered_at >= CAST(? AS TIMESTAMP) "
        f"AND ordered_at < CAST(? AS TIMESTAMP) + {width}), "
        f"(SELECT {aggregate} FROM orders WHERE ordered_at >= CAST(? AS TIMESTAMP) - {back} "
        f"AND ordered_at < CAST(? AS TIMESTAMP) - {back} + {width})"
    )
    out = []
    for row in rows:
        bucket = row[f"{ROLE}__{grain}"]
        ended, value, earlier = _sql(runtime, reference, [bucket, _utc(now), *[bucket] * 4])
        assert ended, row
        assert _number(row[current["as"]]) == _number(value), row
        assert _number(row[prior["as"]]) == _number(earlier), row
        out.append((bucket[:10], _number(value), _number(earlier)))
    assert out
    return out


@pytest.mark.parametrize(("grain", "clock", "phrasing", "window"), MATRIX)
def test_a_comparison_is_ready_only_over_complete_periods(
    live: Callable[..., Runtime],
    monkeypatch: pytest.MonkeyPatch,
    grain: str,
    clock: str,
    phrasing: str,
    window: str,
) -> None:
    now = CLOCKS[grain][clock]
    runtime = live(now)
    intent = PHRASINGS[phrasing].format(unit=grain) + WINDOWS[window][grain]
    partial = {"policy_context": {"now": now}}
    payload = plan_payload(runtime, intent=intent, partial_query=partial)
    before = _unchecked(monkeypatch, runtime, intent, partial)

    # The check holds a draft; it never changes one.
    query = (before["best"] or {}).get("query_ir")
    assert (payload["best"] or {}).get("query_ir") == query
    incomplete = query is None or _reaches_incomplete(runtime, query.get("time") or {}, now)
    if incomplete:
        # Every question here compares periods: one reaching a period in progress never runs.
        assert not _ready(payload), payload
    if query is not None and _compares(query) and incomplete:
        details = _assert_held(payload)
        complete = {**query, "time": {**query["time"], "end": details["complete_end"]}}
        _complete_rows(runtime, complete, now)
        return
    # Everything else keeps its answer: status, reason, readiness and draft.
    for key in ("status", "why", "next", "best", "warnings"):
        assert payload.get(key) == before.get(key), key
    if query is not None and _compares(query):
        # A comparison over a window that has ended runs as before, on complete periods.
        _complete_rows(runtime, query, now)


@pytest.mark.parametrize(
    ("intent", "alias", "so_far", "complete"),
    [
        # Before this check, each was ready and its last row put July so far beside all of June.
        ("revenue month over month", "revenue", (9, 3), (3, 6)),
        ("revenue vs last month", "revenue", (9, 3), (3, 6)),
        ("orders month over month", "order_count", (1, 1), (1, 2)),
    ],
)
def test_the_month_so_far_is_never_put_beside_a_full_month(
    live: Callable[..., Runtime],
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    alias: str,
    so_far: tuple[int, int],
    complete: tuple[int, int],
) -> None:
    runtime = live(NOW)
    partial = {"policy_context": {"now": NOW}}
    payload = plan_payload(runtime, intent=intent, partial_query=partial)

    details = _assert_held(payload)
    assert details["incomplete_period"] == {"start": "2024-07-01", "end": "2024-08-01"}
    assert details["complete_end"] == "2024-07-01"
    assert "July 2024" in payload["why"]["message"]
    assert payload["why"]["recovery_hints"][0]["message"] == (
        "Compare complete months through June 2024: set query.time.end to 2024-07-01 "
        "(end-exclusive), then validate."
    )
    before = _unchecked(monkeypatch, runtime, intent, partial)
    assert before["status"] == "ok" and _ready(before)
    query = payload["best"]["query_ir"]
    assert query == before["best"]["query_ir"] and "end" not in query["time"]
    last = runtime.query({**query, "policy_context": {"now": NOW}})["rows"][-1]
    prior = query["select"][1]["as"]
    assert last[f"{ROLE}__month"].startswith("2024-07-01")
    assert (_number(last[alias]), _number(last[prior])) == so_far
    # The alternative ends with June beside May, both complete.
    rows = _complete_rows(runtime, {**query, "time": {**query["time"], "end": "2024-07-01"}}, NOW)
    assert rows[-1] == ("2024-06-01", *map(Decimal, complete))
    # Following the hint makes the comparison ready.
    bounded = plan_payload(
        runtime, intent=intent, partial_query={**partial, "time": {"end": "2024-07-01"}}
    )
    assert bounded["status"] == "ok" and _ready(bounded)


def test_a_dropped_start_never_asks_to_execute_an_incomplete_comparison(
    live: Callable[..., Runtime],
) -> None:
    """ "this month vs last month" can't start at July: keeping its rows from July on would
    still return July so far, so the hint names the complete months instead."""

    payload = plan_payload(
        live(NOW),
        intent="revenue this month vs last month",
        partial_query={"policy_context": {"now": NOW}},
    )
    details = _assert_held(payload)
    assert details["requested_start"] == "2024-07-01"
    assert payload["best"]["query_ir"]["time"]["end"] == "2024-08-01"
    [hint] = payload["why"]["recovery_hints"]
    assert hint["message"] == (
        "No month from 2024-07-01 on is complete yet. Compare complete months through June "
        "2024 instead: set query.time.end to 2024-07-01 (end-exclusive), then validate."
    )
    assert "best.query_ir" not in str(payload["why"])


def test_a_window_holding_now_keeps_the_rows_from_its_start(
    live: Callable[..., Runtime],
) -> None:
    payload = plan_payload(
        live(NOW),
        intent="revenue month over month in 2024",
        partial_query={"policy_context": {"now": NOW}},
    )
    details = _assert_held(payload)
    assert details["requested_start"] == "2024-01-01"
    assert payload["why"]["recovery_hints"][0]["message"] == (
        "Compare complete months through June 2024: set query.time.end to 2024-07-01 "
        "(end-exclusive), then validate; keep the rows dated 2024-01-01 or later."
    )


_CALLER_SELECT = [
    {"expression": {"measure": REVENUE}, "as": "revenue"},
    {"expression": PRIOR_MONTH, "as": "revenue_prior_month"},
]
_RATIO = [
    {
        "expression": {
            "kind": "arithmetic",
            "op": "divide",
            "left": {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"},
            "right": PRIOR_MONTH,
        },
        "as": "growth",
    }
]


@pytest.mark.parametrize(
    ("variant", "now", "intent", "partial", "ready"),
    [
        # A caller's prior-period select on a question that compares nothing.
        pytest.param("utc_authored", NOW, "revenue by month", {"select": _CALLER_SELECT}, False),
        pytest.param(
            "utc_authored",
            NOW,
            "revenue by month",
            {"select": _CALLER_SELECT, "time": {"temporal_role": ROLE, "grain": "month"}},
            False,
            id="caller-time-without-end",
        ),
        pytest.param(
            "utc_authored",
            NOW,
            "revenue by month",
            {
                "select": _CALLER_SELECT,
                "time": {"temporal_role": ROLE, "grain": "month", "end": "2024-07-01"},
            },
            True,
        ),
        # A ratio over a shifted period compares periods too.
        pytest.param("utc_authored", NOW, "revenue by month", {"select": _RATIO}, False),
        # A week bucket crossing the month end is cut short by time.end.
        pytest.param(
            "utc_authored",
            NOW,
            "revenue week over week",
            {"time": {"temporal_role": ROLE, "grain": "week", "end": "2024-06-01"}},
            False,
            id="week-cut-short",
        ),
        pytest.param(
            "utc_authored",
            NOW,
            "revenue week over week",
            {"time": {"temporal_role": ROLE, "grain": "week", "end": "2024-06-03"}},
            True,
            id="week-on-a-monday",
        ),
        # now exactly on the boundary, one second before it, and a date.
        pytest.param(
            "utc_authored",
            "2024-07-01T00:00:00Z",
            "revenue month over month",
            {"time": {"end": "2024-07-01"}},
            True,
        ),
        pytest.param(
            "utc_authored",
            "2024-06-30T23:59:59Z",
            "revenue month over month",
            {"time": {"end": "2024-07-01"}},
            False,
        ),
        pytest.param(
            "utc_authored",
            "2024-07-01",
            "revenue month over month",
            {"time": {"end": "2024-07-01"}},
            True,
        ),
        # The role's zone cuts the buckets: 02:00 UTC on July 1 is June 30 in New York.
        pytest.param(
            "ny_authored",
            "2024-07-01T02:00:00Z",
            "revenue month over month",
            {"time": {"end": "2024-07-01"}},
            False,
        ),
        pytest.param(
            "utc_authored",
            "2024-07-01T02:00:00Z",
            "revenue month over month",
            {"time": {"end": "2024-07-01"}},
            True,
        ),
        # An end with another offset is 22:00 on June 30 in UTC.
        pytest.param(
            "utc_authored",
            NOW,
            "revenue month over month",
            {"time": {"end": "2024-07-01T00:00:00+02:00"}},
            False,
        ),
        # A where bound on a date can cut a period short; another calendar's ends aren't read.
        pytest.param(
            "utc_authored",
            NOW,
            "revenue month over month",
            {
                "time": {"end": "2024-07-01"},
                "where": [
                    {"field": "dimension.shop_order_ordered_at", "op": "<", "value": "2024-06-15"}
                ],
            },
            False,
            id="date-where-bound",
        ),
        pytest.param(
            "utc_authored",
            NOW,
            "revenue month over month",
            {"time": {"end": "2024-07-01", "calendar_id": "fiscal", "fill": True}},
            False,
            id="fiscal-calendar",
        ),
    ],
)
def test_a_caller_query_goes_through_the_same_check(
    live: Callable[..., Runtime],
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    now: str,
    intent: str,
    partial: dict[str, Any],
    ready: bool,
) -> None:
    runtime = live(now, variant)
    request = {**partial, "policy_context": {"now": now}}
    payload = plan_payload(runtime, intent=intent, partial_query=request)
    if ready:
        assert payload["status"] == "ok" and _ready(payload), payload.get("why")
        if len(payload["best"]["query_ir"]["select"]) == 2 and variant == "utc_authored":
            _complete_rows(runtime, payload["best"]["query_ir"], now)
        return
    _assert_held(payload)
    # Nothing else holds it: without the check, plan called it ready.
    assert _ready(_unchecked(monkeypatch, runtime, intent, request))


def test_a_ratio_over_complete_months_matches_reference_sql(live: Callable[..., Runtime]) -> None:
    runtime = live(NOW)
    payload = plan_payload(
        runtime,
        intent="revenue by month",
        partial_query={
            "select": _RATIO,
            "time": {"temporal_role": ROLE, "grain": "month", "end": "2024-07-01"},
            "policy_context": {"now": NOW},
        },
    )
    assert payload["status"] == "ok" and _ready(payload), payload.get("why")
    rows = runtime.query({**payload["best"]["query_ir"], "policy_context": {"now": NOW}})["rows"]
    june = next(row for row in rows if row[f"{ROLE}__month"].startswith("2024-06-01"))
    (expected,) = _sql(
        runtime,
        "SELECT SUM(amount) FILTER (WHERE ordered_at >= TIMESTAMP '2024-06-01') / "
        "SUM(amount) FILTER (WHERE ordered_at < TIMESTAMP '2024-06-01') FROM orders "
        "WHERE ordered_at >= TIMESTAMP '2024-05-01' AND ordered_at < TIMESTAMP '2024-07-01'",
        [],
    )
    assert Decimal(str(june["growth"])) == Decimal(str(float(expected)))


def test_an_injected_draft_goes_through_the_same_check(
    live: Callable[..., Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever generator produced the draft, the readiness check reads its Query IR."""

    def injected(*args: Any, **kwargs: Any) -> Any:
        result = compose(*args, **kwargs)
        draft = replace(result.draft, query={**result.draft.query, "select": _RATIO})
        return replace(result, draft=draft)

    monkeypatch.setattr(plan_module, "compose", injected)
    payload = plan_payload(
        live(NOW), intent="revenue by month", partial_query={"policy_context": {"now": NOW}}
    )
    assert payload["best"]["query_ir"]["select"] == _RATIO
    _assert_held(payload)


METRIC = "metric.sales.month_over_month_revenue_growth"
JAFFLE_ROLE = "temporal_role.jaffle_order_time"


@pytest.mark.parametrize("end", [None, "2018-01-01"])
def test_a_metric_that_compares_periods_is_checked_on_every_plan_path(
    runtime_factory: Callable[[str], Runtime], end: str | None
) -> None:
    """The governed growth metric divides by its prior month: plan and the granted-metric
    shortcut both hold it until its window ends on a month that has ended."""

    runtime = runtime_factory("jaffle_shop")
    time = {"temporal_role": JAFFLE_ROLE, "grain": "month", **({"end": end} if end else {})}
    grant = RequestContext(
        actor="subject",
        roles=("analyst",),
        audience="finance",
        metric_allowlist=(METRIC,),
        dimension_allowlist=(JAFFLE_ROLE,),
    ).to_policy_context()
    try:
        planned = plan_payload(
            runtime,
            intent="month over month revenue growth by month",
            partial_query={"time": {"end": end}} if end else None,
        )
        granted = plan_payload(
            runtime,
            intent="month-over-month revenue growth",
            partial_query={"policy_context": grant, "time": time},
        )
    finally:
        runtime.close()
    for payload in (planned, granted):
        assert METRIC in str(payload["best"]["query_ir"]["select"])
        if end:
            assert payload["status"] == "ok" and _ready(payload), payload.get("why")
        else:
            _assert_held(payload)
    assert granted["best"]["pattern"] == "granted_metric"


# Comparisons on the jaffle package, whose windows run to the wall clock's month, still in
# progress, with what answered them before this check: ready (None) or the code that held them.
JAFFLE_MOVES = [
    ("revenue vs last month", None),
    ("monthly revenue with YoY", None),
    ("Monthly revenue vs prior year", None),
    ("month over month revenue growth by month", None),
    ("revenue with YoY", "PLAN_UNASKED_GROUPING"),
    ("revenue year over year", "PLAN_UNASKED_GROUPING"),
    ("revenue vs last year", "PLAN_UNASKED_GROUPING"),
    ("revenue compared to last year", "PLAN_UNASKED_GROUPING"),
    ("revenue vs prior year by store", "PLAN_UNMATCHED_TERMS"),
    ("revenue this month vs last month", "TIME_WINDOW_START_DROPPED"),
]


@pytest.mark.parametrize(("intent", "before"), JAFFLE_MOVES)
def test_a_comparison_running_to_now_is_held_and_one_over_ended_months_is_unchanged(
    runtime_factory: Callable[[str], Runtime],
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    before: str | None,
) -> None:
    ended = {"time": {"end": "2018-01-01"}}
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
        unchecked = _unchecked(monkeypatch, runtime, intent, None)
        over_ended = plan_payload(runtime, intent=intent, partial_query=ended)
        unchecked_over_ended = _unchecked(monkeypatch, runtime, intent, ended)
    finally:
        runtime.close()
    _assert_held(payload)
    assert _ready(unchecked) is (before is None)
    assert (unchecked.get("why") or {}).get("code") == before
    for key in ("status", "why", "next", "best", "warnings"):
        assert over_ended.get(key) == unchecked_over_ended.get(key), key


def _metric(metric_id: str, expression: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=metric_id, expression=parse_semantic_expression(expression, context="config")
    )


# A growth metric dividing by its prior month, one built on it, and one comparing nothing.
_CONFIG = SimpleNamespace(
    temporal_roles=[SimpleNamespace(id="role", timezone="UTC")],
    dimensions=[],
    metric_recipes=[
        _metric(
            "metric.growth",
            {
                "kind": "arithmetic",
                "op": "divide",
                "left": {"kind": "aggregate", "measure": "m", "aggregation": "sum"},
                "right": {
                    "kind": "prior_period",
                    "input": {"kind": "aggregate", "measure": "m", "aggregation": "sum"},
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


@pytest.mark.parametrize(
    ("grain", "end", "now", "complete_end"),
    [
        ("minute", None, "2024-07-15T12:00:30Z", "2024-07-15T12:00:00"),
        ("hour", "2024-07-15T12:00:00", "2024-07-15T12:00:00Z", None),
        ("hour", "2024-07-15T12:00:00", "2024-07-15T11:59:59Z", "2024-07-15T11:00:00"),
        ("hour", "2024-07-15T11:30:00", "2024-07-15T12:00:00Z", "2024-07-15T11:00:00"),
        # A date is its midnight: that day has not ended.
        ("day", "2024-07-15", "2024-07-15", None),
        ("day", "2024-07-16", "2024-07-15", "2024-07-15"),
        # Weeks start on Monday; 2024-07-14 is a Sunday.
        ("week", "2024-07-15", "2024-07-15T00:00:00Z", None),
        ("week", "2024-07-14", "2024-07-20T00:00:00Z", "2024-07-08"),
        ("quarter", "2024-07-01", "2024-08-15T00:00:00Z", None),
        ("quarter", "2024-08-01", "2024-08-15T00:00:00Z", "2024-07-01"),
        ("year", None, "2024-01-01T00:00:00Z", "2024-01-01"),
    ],
)
def test_a_period_is_complete_when_its_bucket_has_ended_by_now(
    grain: str, end: str | None, now: str, complete_end: str | None
) -> None:
    time = {"temporal_role": "role", "grain": grain, **({"end": end} if end else {})}
    for select in (
        {"expression": {"kind": "prior_period", "measure": "m", "offset": -1, "grain": "day"}},
        {"expression": {"metric": "metric.growth_pct"}},
    ):
        why = incomplete_period_why(
            _CONFIG, {"select": [select], "time": time}, policy_context={"now": now}
        )
        if complete_end is None:
            assert why is None
        else:
            assert why is not None and why["code"] == HELD
            assert why["details"]["complete_end"] == complete_end


@pytest.mark.parametrize(
    "select",
    [{"expression": {"measure": "m"}}, {"expression": {"metric": "metric.total"}}],
)
def test_a_draft_that_compares_nothing_is_not_checked(select: dict[str, Any]) -> None:
    query = {"select": [select], "time": {"temporal_role": "role", "grain": "month"}}
    assert incomplete_period_why(_CONFIG, query, policy_context={"now": NOW}) is None


def test_a_clock_that_cannot_be_read_holds_a_comparison() -> None:
    query = {
        "select": [{"expression": {"metric": "metric.growth"}}],
        "time": {"temporal_role": "role", "grain": "month", "end": "2018-01-01"},
    }
    why = incomplete_period_why(_CONFIG, query, policy_context={"now": "next tuesday"})
    assert why is not None and why["code"] == HELD
    assert why["details"] == {"path": "policy_context.now"}
