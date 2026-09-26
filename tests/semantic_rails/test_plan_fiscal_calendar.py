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
from semantic_rails.planner._base import _time_bounds_from_text, _unresolved_time_phrases
from semantic_rails.planner.faithfulness import intent_faithfulness_why
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

REVENUE = {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"


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


def test_a_dropped_fiscal_comparison_is_reported(jaffle: Runtime, no_fiscal: Runtime) -> None:
    question = "revenue by fiscal quarter vs prior fiscal quarter"
    for runtime in (jaffle, no_fiscal):
        payload = plan_payload(runtime, intent=question)
        assert payload["status"] == "low_confidence"
        assert "prior_period_comparison_unrealized" in _gap_kinds(payload)


def test_a_prior_fiscal_year_compares_fiscal_periods(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="revenue vs prior fiscal year")
    query = payload["best"]["query_ir"]

    assert query["time"]["calendar_id"] == "fiscal"
    assert [item["expression"].get("kind") for item in query["select"]] == [None, "prior_period"]


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
    assert payload["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert "query_ir" not in payload["best"]
    assert "exact days" in payload["why"]["recovery_hints"][0]["message"]


def test_a_window_total_is_not_split_into_fiscal_buckets(jaffle: Runtime) -> None:
    # plan picks quarter to hold January to March in one Gregorian bucket; fiscal quarters
    # would split it into two partial ones, so plan reports the gap instead.
    payload = plan_payload(jaffle, intent="fiscal revenue from 2017-01-01 to 2017-03-31")
    assert payload["status"] == "low_confidence"
    assert _gap_kinds(payload) == ["fiscal_calendar_unrealized"]
    assert "'fiscal'" in payload["why"]["recovery_hints"][0]["message"]

    # A day is a day on any calendar.
    day = plan_payload(jaffle, intent="fiscal revenue on April 3, 2017")
    assert day["status"] == "ok"
    assert day["best"]["query_ir"]["time"]["grain"] == "day"


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
    ("time", "group_by", "gap"),
    [
        ({"temporal_role": ORDER_TIME, "grain": "quarter"}, [], True),
        ({"temporal_role": ORDER_TIME, "grain": "quarter", "calendar_id": "default"}, [], True),
        ({"temporal_role": ORDER_TIME, "grain": "quarter", "calendar_id": "fiscal"}, [], False),
        # A caller may name any non-default calendar; plan picks only the fiscal one itself.
        ({"temporal_role": ORDER_TIME, "grain": "quarter", "calendar_id": "retail"}, [], False),
        # An object whose name says fiscal answers it too.
        (None, ["dimension.jaffle_fiscal_calendar_quarter_start"], False),
    ],
)
def test_the_fiscal_gap_reads_the_drafted_query(
    jaffle: Runtime, time: dict[str, Any] | None, group_by: list[str], gap: bool
) -> None:
    question = "revenue by fiscal quarter"
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
