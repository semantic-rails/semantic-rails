"""plan counts a fiscal question's time on the fiscal calendar, or says it can't.

"Revenue by fiscal quarter" used to come back as Gregorian quarters with status
ok, "vs prior fiscal quarter" was dropped with status ok, and "fiscal Q2 2017"
resolved to April through June. Now a package's one fiscal calendar buckets
the draft; without one, or for a window only the calendar could date, plan
reports the gap instead of answering a different question.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import (
    _time_bounds_from_text,
    _unresolved_time_phrases,
    _with_fiscal_calendar,
)
from semantic_rails.planner.faithfulness import intent_faithfulness_why
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

REVENUE = {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
QUARTERS = {"temporal_role": ORDER_TIME, "grain": "quarter"}
FISCAL_QUARTERS = {**QUARTERS, "calendar_id": "fiscal", "fill": True}
FISCAL_Q1_2017 = {**FISCAL_QUARTERS, "start": "2017-02-01", "end": "2017-05-01"}
FISCAL_QUARTER_START = "dimension.jaffle_fiscal_calendar_quarter_start"


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
        yield runtime
    finally:
        runtime.close()


@pytest.fixture(scope="module")
def jaffle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    yield from _runtime(tmp_path_factory, fiscal=True)


@pytest.fixture(scope="module")
def no_fiscal(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    yield from _runtime(tmp_path_factory, fiscal=False)


def _gap_kinds(payload: dict[str, Any]) -> list[str]:
    return [gap["kind"] for gap in (payload.get("why") or {}).get("details", {}).get("gaps", [])]


@pytest.mark.parametrize(
    ("question", "grain", "group_by"),
    [
        ("revenue by fiscal quarter", "quarter", None),
        ("fiscal quarterly revenue", "quarter", None),
        ("revenue by fiscal year", "year", None),
        ("revenue by fiscal month", "month", None),
        ("revenue by store by fiscal quarter", "quarter", [STORE]),
    ],
)
def test_a_fiscal_series_buckets_on_the_fiscal_calendar(
    jaffle: Runtime, question: str, grain: str, group_by: list[str] | None
) -> None:
    payload = plan_payload(jaffle, intent=question)
    query = payload["best"]["query_ir"]

    assert payload["status"] == "ok", payload.get("why")
    assert "warnings" not in payload
    assert query["time"] == {
        "temporal_role": ORDER_TIME,
        "grain": grain,
        "calendar_id": "fiscal",
        "fill": True,
    }
    # "fiscal quarter" read as the calendar's own column is the time bucket now.
    assert query.get("group_by") == group_by


def test_fiscal_quarters_start_where_the_fiscal_calendar_starts_them(jaffle: Runtime) -> None:
    query = plan_payload(jaffle, intent="revenue by fiscal quarter")["best"]["query_ir"]
    rows = jaffle.query(query)["rows"]
    starts = [row[f"{ORDER_TIME}__quarter"] for row in rows]

    # The fixture's fiscal year starts in February; Gregorian quarters start in January.
    assert {start.month for start in starts} == {2, 5, 8, 11}
    assert sum(row["revenue_usd"] for row in rows) == pytest.approx(745893.03, abs=0.01)


@pytest.mark.parametrize(
    "question",
    ["revenue by fiscal quarter vs prior fiscal quarter", "revenue vs prior fiscal year"],
)
def test_a_fiscal_comparison_is_reported(
    jaffle: Runtime, no_fiscal: Runtime, question: str
) -> None:
    # plan compares only Gregorian periods, so it leaves the draft off the fiscal calendar.
    for runtime in (jaffle, no_fiscal):
        payload = plan_payload(runtime, intent=question)
        assert payload["status"] == "low_confidence"
        assert "calendar_id" not in payload["best"]["query_ir"]["time"]
    payload = plan_payload(no_fiscal, intent=question)
    assert "prior_period_comparison_unrealized" in _gap_kinds(payload)


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
    ("question", "phrase"),
    [
        ("revenue in fiscal Q2 2017", "q2 2017"),
        ("revenue by fiscal quarter in 2017", "in 2017"),
        ("revenue in fiscal 2017", "fiscal 2017"),
        ("revenue in FY2017", "fy2017"),
        ("revenue last fiscal quarter", "last fiscal quarter"),
        ("revenue this fiscal year", "this fiscal year"),
        ("revenue by fiscal month for the last 3 months", "last 3 months"),
    ],
)
def test_a_fiscal_window_resolves_only_from_exact_days(
    jaffle: Runtime, question: str, phrase: str
) -> None:
    assert _time_bounds_from_text(question) == {}
    assert phrase in _unresolved_time_phrases(question)

    payload = plan_payload(jaffle, intent=question)
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED" or (
        "time_window_unresolved" in _gap_kinds(payload)
    )
    assert "query_ir" not in payload["best"]
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
    payload = plan_payload(jaffle, intent=question)

    assert payload["status"] == "low_confidence"
    assert "calendar_id" not in payload["best"]["query_ir"].get("time", {})


def test_the_fiscal_gap_says_what_the_draft_lacks(jaffle: Runtime) -> None:
    window = plan_payload(jaffle, intent="fiscal revenue from 2017-01-01 to 2017-03-31")
    assert _gap_kinds(window) == ["fiscal_calendar_unrealized"]
    assert window["why"]["recovery_hints"][0]["message"].startswith(
        "Set query.time.calendar_id to 'fiscal' and time.fill to true, then"
    )

    period = plan_payload(jaffle, intent="revenue in the first fiscal quarter")
    assert _gap_kinds(period) == ["fiscal_calendar_unrealized"]
    assert "exact query.time.start and end" in period["why"]["recovery_hints"][0]["message"]

    no_time = plan_payload(jaffle, intent="fiscal revenue by store")
    assert "with a temporal_role and grain" in no_time["why"]["recovery_hints"][0]["message"]

    # A day is a day on any calendar.
    day = plan_payload(jaffle, intent="fiscal revenue on April 3, 2017")
    assert day["status"] == "ok"
    assert day["best"]["query_ir"]["time"]["grain"] == "day"


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
    # to-date or rolling ask; bucketing fiscal months would turn a flagged draft into an ok
    # one. The step leaves the draft alone whatever the package can reach.
    draft = {
        "version": 2,
        "select": [REVENUE],
        "time": {"temporal_role": ORDER_TIME, "grain": "month"},
    }
    assert _with_fiscal_calendar(jaffle._config, question, draft) == draft

    payload = plan_payload(jaffle, intent=question)
    assert payload["status"] == "low_confidence"
    assert "calendar_id" not in payload["best"]["query_ir"].get("time", {})


def test_an_order_on_the_fiscal_bucket_orders_by_time(jaffle: Runtime) -> None:
    draft = {
        "version": 2,
        "select": [REVENUE],
        "group_by": [FISCAL_QUARTER_START, STORE],
        "order_by": [
            {"field": FISCAL_QUARTER_START, "direction": "DESC"},
            {"field": "revenue_usd", "direction": "DESC"},
        ],
        "time": QUARTERS,
    }
    query = _with_fiscal_calendar(jaffle._config, "revenue by fiscal quarter", draft)

    assert query["time"] == FISCAL_QUARTERS
    assert query["group_by"] == [STORE]
    assert query["order_by"] == [
        {"field": "time", "direction": "DESC"},
        {"field": "revenue_usd", "direction": "DESC"},
    ]


def test_a_caller_time_block_keeps_the_draft_off_the_fiscal_calendar(jaffle: Runtime) -> None:
    # The caller's grain would replace the fiscal quarter the step chose.
    payload = plan_payload(
        jaffle, intent="revenue by fiscal quarter", partial_query={"time": {"grain": "month"}}
    )

    assert payload["status"] == "low_confidence"
    assert "calendar_id" not in payload["best"]["query_ir"]["time"]


def test_exact_days_bound_a_fiscal_series(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="revenue by fiscal quarter from 2017-02-01 to 2018-01-31")
    time = payload["best"]["query_ir"]["time"]

    assert payload["status"] == "ok"
    assert (time["calendar_id"], time["start"], time["end"]) == (
        "fiscal",
        "2017-02-01",
        "2018-02-01",
    )


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
        ("revenue by fiscal quarter", {**QUARTERS, "calendar_id": "default"}, [], True),
        ("revenue by fiscal quarter", FISCAL_QUARTERS, [], False),
        # A caller may name any non-default calendar; plan picks only the fiscal one itself.
        ("revenue by fiscal quarter", {**QUARTERS, "calendar_id": "retail"}, [], False),
        # A dimension whose name says fiscal answers it too.
        ("revenue by fiscal quarter", None, [FISCAL_QUARTER_START], False),
        ("revenue by fiscal quarter", QUARTERS, [FISCAL_QUARTER_START], False),
        # A fiscal period needs its exact days as well as the calendar.
        ("revenue in the first fiscal quarter", FISCAL_QUARTERS, [], True),
        ("revenue in the first fiscal quarter", FISCAL_Q1_2017, [], False),
        ("revenue in the first fiscal quarter", QUARTERS, [FISCAL_QUARTER_START], True),
        # Days are the same on any calendar, but the question asks for fiscal quarters.
        ("daily revenue by fiscal quarter", {**QUARTERS, "grain": "day"}, [], True),
        (
            "fiscal revenue from 2017-02-01 to 2017-02-28",
            {**FISCAL_Q1_2017, "grain": "day"},
            [],
            False,
        ),
        # Period-to-date resets on Gregorian periods whatever the calendar.
        ("fiscal ytd revenue by fiscal quarter", FISCAL_Q1_2017, [], True),
    ],
)
def test_the_fiscal_gap_reads_the_drafted_query(
    jaffle: Runtime,
    question: str,
    time: dict[str, Any] | None,
    group_by: list[str],
    gap: bool,
) -> None:
    query: dict[str, Any] = {"version": 2, "select": [REVENUE], "group_by": group_by}
    if time is not None:
        query["time"] = time
    why = intent_faithfulness_why(
        jaffle, question=question, intent_ir=parse_intent(jaffle, question), query=query
    )
    kinds = [row["kind"] for row in (why or {}).get("details", {}).get("gaps", [])]

    assert ("fiscal_calendar_unrealized" in kinds) is gap


def test_a_gregorian_question_is_unchanged(jaffle: Runtime) -> None:
    query = plan_payload(jaffle, intent="revenue by quarter")["best"]["query_ir"]

    assert query["time"] == {"temporal_role": ORDER_TIME, "grain": "quarter"}


@pytest.mark.parametrize(
    "question", ["fiscally prudent revenue in 2017", "revenue from fyre stores in 2017"]
)
def test_only_the_whole_word_names_a_fiscal_calendar(question: str) -> None:
    assert _time_bounds_from_text(question) == {"start": "2017-01-01", "end": "2018-01-01"}
