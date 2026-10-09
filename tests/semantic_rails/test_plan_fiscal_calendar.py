"""plan holds a fiscal question: this release reads no fiscal calendar.

"Revenue by fiscal quarter" used to come back as Gregorian quarters with status
ok, "vs prior fiscal quarter" was dropped with status ok, and "fiscal Q2 2017"
resolved to April through June. A call names no calendar, so plan never answers a
fiscal series, comparison, period or to-date value in Gregorian periods: it holds
the draft with a gap that says the fiscal calendar isn't read. A Gregorian
question is unchanged.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.planner.call import Call
from semantic_rails.planner.faithfulness import intent_faithfulness_why
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.patterns.metric_by_dimension_rollup import _unresolved_time_phrases
from semantic_rails.planner.time_windows import _time_bounds_from_text
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

REVENUE = "measure.jaffle.revenue_usd"
ORDER_TIME = "temporal_role.jaffle_order_time"
QUARTERS = {"temporal_role": ORDER_TIME, "grain": "quarter"}
# The fiscal first quarter of 2017, as exact days.
FISCAL_Q1_DAYS = {"start": "2017-02-01", "end": "2017-05-01"}
# The fiscal calendar's own column, which no order reaches.
FISCAL_QUARTER_START = "dimension.jaffle_fiscal_calendar_quarter_start"
# A fiscal-period column on the orders themselves (added by the ``fiscal_column`` fixture).
FISCAL_COLUMN = "dimension.jaffle_order_fiscal_quarter"
# What a fiscal hold's hint says: the release reads no fiscal calendar.
NOT_READ = "reads no fiscal calendar"
# How plan holds a fiscal question: by its fiscal gap, or, when the draft groups by a calendar
# column the orders can't reach, by validation or the fallback's drift. Never ok.
HELD_BY = {"PLAN_INTENT_COVERAGE_GAP", "VALIDATION_FAILED", "PLAN_FALLBACK_SEMANTIC_DRIFT"}


COMPARISON_PACKAGE = (
    Path(__file__).resolve().parents[2] / "comparisons/semantic_layers/semantic_rails/package"
)


def _runtime(tmp_path_factory: pytest.TempPathFactory, *, fiscal: bool) -> Iterator[Runtime]:
    if fiscal:
        path = copy_package_config(
            tmp_path_factory.mktemp("fiscal"), "jaffle_shop", preseed_db=True
        )
    else:
        # The comparison package has no calendar at all; plan only drafts there.
        path = tmp_path_factory.mktemp("no_fiscal") / "package"
        shutil.copytree(COMPARISON_PACKAGE, path)
        raw = yaml.safe_load((path / "package.yml").read_text(encoding="utf-8"))
        raw["package"].update(default_db="comparison.duckdb", seed={"kind": "external"})
        (path / "package.yml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    runtime = Runtime.from_path(str(path))
    try:
        yield opened(runtime) if fiscal else runtime  # The comparison has no database.
    finally:
        runtime.close()


@pytest.fixture(scope="module")
def jaffle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    yield from _runtime(tmp_path_factory, fiscal=True)


@pytest.fixture(scope="module")
def no_fiscal(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    yield from _runtime(tmp_path_factory, fiscal=False)


@pytest.fixture(scope="module")
def fiscal_column(jaffle: Runtime) -> Iterator[Runtime]:
    """Jaffle with a fiscal-quarter column on its orders, which the order measures reach."""
    config = jaffle._config
    number = next(
        row for row in config.dimensions if row.id == "dimension.jaffle_order_customer_order_number"
    )
    column = replace(
        number,
        id=FISCAL_COLUMN,
        name="jaffle.Order.fiscal_quarter",
        label="Fiscal quarter",
        aliases=[],
        description="",
        value_domain="",
    )
    runtime = Runtime.from_config(
        replace(config, dimensions=[*config.dimensions, column]), source_path=jaffle.source_path
    )
    try:
        yield runtime
    finally:
        runtime.close()


def _gap_kinds(payload: dict[str, Any]) -> list[str]:
    return [gap["kind"] for gap in (payload.get("why") or {}).get("details", {}).get("gaps", [])]


def _hints(payload: dict[str, Any]) -> str:
    return " ".join(
        hint["message"] for hint in (payload.get("why") or {}).get("recovery_hints", [])
    )


def _assert_fiscal_held(payload: dict[str, Any], says: str = "") -> None:
    """Held, never ready. Held by the coverage check, the fiscal gap is among its gaps and
    its hint says ``says``."""
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
    code = payload["why"]["code"]
    assert code in HELD_BY, payload["why"]
    if code == "PLAN_INTENT_COVERAGE_GAP":
        assert "fiscal_calendar_unrealized" in _gap_kinds(payload), payload["why"]
        assert says in _hints(payload), _hints(payload)


@pytest.mark.parametrize(
    "question",
    [
        "revenue by fiscal quarter",
        "fiscal quarterly revenue",
        "revenue by fiscal year",
        "revenue by fiscal month",
        "revenue by store by fiscal quarter",
        "revenue by FY quarter",
    ],
)
def test_a_fiscal_series_is_held_never_answered_in_gregorian_periods(
    jaffle: Runtime, question: str
) -> None:
    _assert_fiscal_held(plan_payload(jaffle, intent=question), NOT_READ)


@pytest.mark.parametrize(
    "question",
    ["revenue by fiscal quarter vs prior fiscal quarter", "revenue vs prior fiscal year"],
)
def test_a_fiscal_comparison_is_reported(
    jaffle: Runtime, no_fiscal: Runtime, question: str
) -> None:
    # plan compares only Gregorian periods: the fiscal comparison is held, never answered.
    for runtime in (jaffle, no_fiscal):
        _assert_fiscal_held(plan_payload(runtime, intent=question), NOT_READ)
    payload = plan_payload(no_fiscal, intent=question)
    assert {"prior_period_comparison_unrealized", "fiscal_calendar_unrealized"} <= set(
        _gap_kinds(payload)
    )


@pytest.mark.parametrize(
    "question",
    ["revenue by fiscal quarter", "fiscal revenue by store", "revenue by FY quarter"],
)
def test_without_a_fiscal_calendar_plan_reports_the_gap(no_fiscal: Runtime, question: str) -> None:
    payload = plan_payload(no_fiscal, intent=question)

    assert payload["status"] == "low_confidence"
    assert _gap_kinds(payload) == ["fiscal_calendar_unrealized"]
    assert "calendar" in payload["why"]["recovery_hints"][0]["message"]


@pytest.mark.parametrize(
    ("question", "phrase", "fiscal_series"),
    [
        ("revenue in fiscal Q2 2017", "q2 2017", False),
        ("revenue by fiscal quarter in 2017", "in 2017", True),
        ("revenue in fiscal 2017", "fiscal 2017", False),
        ("revenue in FY2017", "fy2017", False),
        ("revenue last fiscal quarter", "last fiscal quarter", False),
        ("revenue this fiscal year", "this fiscal year", False),
        ("revenue by fiscal month for the last 3 months", "last 3 months", True),
    ],
)
def test_a_fiscal_window_resolves_only_from_exact_days(
    jaffle: Runtime, question: str, phrase: str, fiscal_series: bool
) -> None:
    assert _time_bounds_from_text(question) == {}
    assert phrase in _unresolved_time_phrases(question)

    payload = plan_payload(jaffle, intent=question)
    if fiscal_series:
        # The fiscal series is held whatever its window.
        _assert_fiscal_held(payload, NOT_READ)
        return
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED" or (
        "time_window_unresolved" in _gap_kinds(payload)
    )
    assert "query" not in payload["best"]
    assert any("exact days" in hint["message"] for hint in payload["why"]["recovery_hints"])


@pytest.mark.parametrize(
    "question",
    [
        # plan picks quarter to hold January to March in one Gregorian bucket; fiscal
        # quarters would split it into two partial ones.
        "fiscal revenue from 2017-01-01 to 2017-03-31",
        # plan picks month to hold February; the question asks for fiscal years.
        "fiscal annual revenue from 2017-02-01 to 2017-02-28",
        # A fiscal period no window cue reads: plan would drop it.
        "revenue in the first fiscal quarter",
        "revenue for the second fiscal quarter",
        "revenue in the latest fiscal quarter",
        "revenue since the start of the fiscal year by fiscal month",
    ],
)
def test_only_a_fiscal_bucket_of_the_drafted_grain_is_honored(
    jaffle: Runtime, question: str
) -> None:
    _assert_fiscal_held(plan_payload(jaffle, intent=question))


def test_the_fiscal_gap_says_what_the_draft_lacks(jaffle: Runtime) -> None:
    window = plan_payload(jaffle, intent="fiscal revenue from 2017-01-01 to 2017-03-31")
    assert _gap_kinds(window) == ["fiscal_calendar_unrealized"]
    assert NOT_READ in _hints(window)

    period = plan_payload(jaffle, intent="revenue in the first fiscal quarter")
    assert _gap_kinds(period) == ["fiscal_calendar_unrealized"]
    assert "exact time.start and end" in _hints(period)

    no_time = plan_payload(jaffle, intent="fiscal revenue by store")
    assert NOT_READ in _hints(no_time)


def test_a_fiscal_question_about_one_day_is_answered(jaffle: Runtime) -> None:
    # A day is a day on any calendar.
    day = plan_payload(jaffle, intent="fiscal revenue on April 3, 2017")
    assert day["status"] == "ok", day.get("why")
    query = day["best"]["query"]
    assert (query["time"]["start"], query["time"]["end"]) == ("2017-04-03", "2017-04-04")
    [row] = jaffle.query(query)["rows"]
    with duckdb.connect(jaffle.db_path, read_only=True) as connection:
        [(reference,)] = connection.execute(
            "SELECT SUM(order_total_cents / 100.0) FROM jaffle_order "
            "WHERE ordered_at >= TIMESTAMP '2017-04-03' AND ordered_at < TIMESTAMP '2017-04-04'"
        ).fetchall()
    assert reference
    assert row["revenue_usd"] == pytest.approx(reference)


@pytest.mark.parametrize(
    "question",
    [
        "year to date revenue by fiscal month",
        "fiscal ytd revenue by month",
        "rolling 3 month revenue by fiscal month",
    ],
)
def test_a_to_date_or_rolling_fiscal_question_is_not_answered(
    jaffle: Runtime, question: str
) -> None:
    # Period-to-date resets on Gregorian periods whatever the calendar, and plan drops the
    # to-date or rolling ask.
    _assert_fiscal_held(plan_payload(jaffle, intent=question), "fiscal to-date or rolling")


def test_exact_days_bound_a_fiscal_series(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="revenue by fiscal quarter from 2017-02-01 to 2018-01-31")
    time = payload["best"]["query"]["time"]

    # The exact days are read; the fiscal buckets still hold the draft.
    _assert_fiscal_held(payload, NOT_READ)
    assert (time["start"], time["end"]) == ("2017-02-01", "2018-02-01")


@pytest.mark.parametrize(
    "question",
    [
        "revenue by fiscal quarter vs prior fiscal quarter",
        "revenue compared to last fiscal year",
    ],
)
def test_a_fiscal_comparison_is_not_a_window(question: str) -> None:
    assert _unresolved_time_phrases(question) == []


@pytest.mark.parametrize(
    ("question", "time", "group_by", "gap"),
    [
        ("revenue by fiscal quarter", QUARTERS, [], True),
        # The fiscal calendar's own column is no bucket the orders reach.
        ("revenue by fiscal quarter", None, [FISCAL_QUARTER_START], True),
        ("revenue by fiscal quarter", QUARTERS, [FISCAL_QUARTER_START], True),
        # A fiscal column the measures reach answers it.
        ("revenue by fiscal quarter", None, [FISCAL_COLUMN], False),
        ("revenue by fiscal quarter", QUARTERS, [FISCAL_COLUMN], False),
        # A fiscal period needs its exact days as well as the fiscal buckets.
        ("revenue in the first fiscal quarter", QUARTERS, [], True),
        ("revenue in the first fiscal quarter", QUARTERS, [FISCAL_COLUMN], True),
        (
            "revenue in the first fiscal quarter",
            {**QUARTERS, **FISCAL_Q1_DAYS},
            [FISCAL_COLUMN],
            False,
        ),
        (
            "revenue in the first fiscal quarter",
            {**QUARTERS, **FISCAL_Q1_DAYS},
            [FISCAL_QUARTER_START],
            True,
        ),
        # Days are the same on any calendar, but the question asks for fiscal quarters.
        ("daily revenue by fiscal quarter", {**QUARTERS, "grain": "day"}, [], True),
        (
            "fiscal revenue from 2017-02-01 to 2017-02-28",
            {**QUARTERS, "grain": "day", "start": "2017-02-01", "end": "2017-03-01"},
            [],
            False,
        ),
        # Period-to-date resets on Gregorian periods whatever the calendar.
        (
            "fiscal ytd revenue by fiscal quarter",
            {**QUARTERS, **FISCAL_Q1_DAYS},
            [FISCAL_COLUMN],
            True,
        ),
    ],
)
def test_the_fiscal_gap_reads_the_drafted_query(
    fiscal_column: Runtime,
    question: str,
    time: dict[str, Any] | None,
    group_by: list[str],
    gap: bool,
) -> None:
    query: dict[str, Any] = {"select": [REVENUE], "group_by": group_by}
    if time is not None:
        query["time"] = time
    why = intent_faithfulness_why(
        fiscal_column,
        question=question,
        intent_ir=parse_intent(fiscal_column, question),
        query=Call.from_call(query),
    )
    kinds = [row["kind"] for row in (why or {}).get("details", {}).get("gaps", [])]

    assert ("fiscal_calendar_unrealized" in kinds) is gap


def test_a_gregorian_question_is_unchanged(jaffle: Runtime) -> None:
    query = plan_payload(jaffle, intent="revenue by quarter")["best"]["query"]

    assert query["time"] == {"temporal_role": ORDER_TIME, "grain": "quarter"}


@pytest.mark.parametrize(
    "question", ["fiscally prudent revenue in 2017", "revenue from fyre stores in 2017"]
)
def test_only_the_whole_word_names_a_fiscal_calendar(question: str) -> None:
    assert _time_bounds_from_text(question) == {"start": "2017-01-01", "end": "2018-01-01"}
