"""Hermetic gates on the query MCP's context cost and the planner's accuracy.

``scripts/mcp_context.py`` does the measuring against a throwaway jaffle_shop
fixture. These tests fail when a measured size exceeds its budget, when a gold
case's planner outcome gets worse, when a gold query's answer changes, or when
the frozen eval set is edited. See "Measuring context cost" in
docs/MCP_INTERFACE.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import mcp_context

# Digest of the frozen dev split, tests/semantic_rails/mcp_context/eval_jaffle.jsonl.
# The set is frozen: change a case only through a reviewed revision of the eval
# set, and update this digest in that same change.
FROZEN_DEV_SET_SHA256 = "3a20d349655a3ff8cee8e533aec2d4cb4e263f93c5981b853f7b1207846f77ad"


@pytest.fixture(scope="module")
def jaffle_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return mcp_context.build_jaffle_fixture(tmp_path_factory.mktemp("mcp_context"))


@pytest.fixture(scope="module")
def dev_cases() -> list[dict[str, Any]]:
    return mcp_context.load_eval_cases()


def _plan_baseline() -> dict[str, Any]:
    baseline: dict[str, Any] = json.loads(
        mcp_context.PLAN_BASELINE_PATH.read_text(encoding="utf-8")
    )
    return baseline


def test_eval_set_is_frozen(dev_cases: list[dict[str, Any]]) -> None:
    assert {case["split"] for case in dev_cases} == {"dev"}
    assert mcp_context.eval_set_digest(dev_cases) == FROZEN_DEV_SET_SHA256
    assert _plan_baseline()["eval_set_sha256"] == FROZEN_DEV_SET_SHA256


def test_query_mcp_stays_within_context_budgets(jaffle_package: Path) -> None:
    metrics = mcp_context.measure_query_mcp(jaffle_package)
    checks = mcp_context.check_budgets(metrics, mcp_context.load_budgets(), prefix="query.")
    failures = [
        f"{check.metric}: measured {check.value}, budget {check.budget} ({check.status})"
        for check in checks
        if check.failed
    ]
    assert not failures, (
        "Query MCP context budgets failed. Raise a budget only for an intended change, "
        "with `uv run python scripts/mcp_context.py --write-baseline`:\n" + "\n".join(failures)
    )


def test_architect_tool_list_is_tracked(tmp_path: Path) -> None:
    metrics = mcp_context.measure_architect_mcp(tmp_path)
    # Tracked for the record, not gated: another workstream owns that server.
    assert set(metrics) == set(mcp_context.load_budgets()["tracked"])
    assert metrics["architect.tools_list.tools"] > 0


def test_planner_accuracy_does_not_regress(
    jaffle_package: Path, dev_cases: list[dict[str, Any]]
) -> None:
    outcomes = mcp_context.run_plan_accuracy(jaffle_package, dev_cases)
    baseline = _plan_baseline()
    regressions, _improvements = mcp_context.plan_regressions(outcomes, baseline)
    assert not regressions, "Planner accuracy regressed:\n" + "\n".join(regressions)
    summary = mcp_context.plan_summary(outcomes)
    assert summary["pass"] >= baseline["summary"]["pass"]
    assert summary["wrong_silent"] <= baseline["summary"]["wrong_silent"]


def test_gold_answers_are_stable(jaffle_package: Path, dev_cases: list[dict[str, Any]]) -> None:
    assert mcp_context.check_gold_answers(jaffle_package, dev_cases) == []


# --- The gates themselves --------------------------------------------------

REVENUE = "measure.jaffle.revenue_usd"
STORE = "dimension.jaffle_store_name"
ORDER_TIME = "temporal_role.jaffle_order_time"
AGGREGATIONS = {REVENUE: "sum"}


def _case(gold: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": "T1", "category": "test", "expect": "answer", "gold_query": gold, **extra}


def _query(**parts: Any) -> dict[str, Any]:
    return {
        "version": 2,
        "select": [{"as": "revenue_usd", "expression": {"measure": REVENUE}}],
        **parts,
    }


def _plan(query: dict[str, Any] | None, *, status: str = "ok", warnings: int = 0) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "warnings": [{"code": "SOME_WARNING"}] * warnings,
        "best": {"query_ir": query} if query is not None else None,
    }


WINDOW_2017 = {
    "temporal_role": ORDER_TIME,
    "grain": "month",
    "start": "2017-01-01",
    "end": "2018-01-01",
}


def test_dropped_window_is_a_silent_wrong_answer_unless_flagged() -> None:
    case = _case(_query(time=WINDOW_2017))
    dropped = _query(time={"temporal_role": ORDER_TIME, "grain": "month"})
    silent = mcp_context.score_plan_response(case, _plan(dropped), AGGREGATIONS)
    assert (silent.outcome, silent.mismatched) == (mcp_context.SILENT, ("window",))
    flagged = mcp_context.score_plan_response(case, _plan(dropped, warnings=1), AGGREGATIONS)
    assert flagged.outcome == mcp_context.FLAGGED
    low_confidence = mcp_context.score_plan_response(
        case, _plan(dropped, status="low_confidence"), AGGREGATIONS
    )
    assert low_confidence.outcome == mcp_context.FLAGGED


def test_multi_value_filter_must_keep_every_value() -> None:
    gold = _query(
        group_by=[STORE],
        where=[{"field": STORE, "op": "in", "value": ["Philadelphia", "Brooklyn"]}],
    )
    one_value = _query(group_by=[STORE], where=[{"field": STORE, "op": "=", "value": "Brooklyn"}])
    reordered = _query(
        group_by=[STORE],
        where=[{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}],
    )
    # Pinning the store to one value also collapses the per-store grouping.
    assert mcp_context.mismatched_slots(_case(gold), one_value, AGGREGATIONS) == [
        "group_by",
        "where",
    ]
    assert mcp_context.mismatched_slots(_case(gold), reordered, AGGREGATIONS) == []


def test_equivalent_spellings_compare_equal() -> None:
    gold = _query(where=[{"field": STORE, "op": "=", "value": "Brooklyn"}], time=WINDOW_2017)
    spelled_out = {
        "version": 2,
        "select": [
            {
                "as": "rev",
                "expression": {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"},
            }
        ],
        # A dimension pinned to one value by a filter doesn't change the answer.
        "group_by": [STORE],
        "where": [{"field": STORE, "op": "in", "value": ["Brooklyn"]}],
        "time": {**WINDOW_2017, "end": "2018-01-01T00:00:00"},
        "order_by": [{"field": "time", "direction": "ASC"}],
    }
    assert mcp_context.mismatched_slots(_case(gold), spelled_out, AGGREGATIONS) == []


def test_ranking_order_matters_only_for_ranking_questions() -> None:
    gold = _query(
        group_by=[STORE], order_by=[{"field": "revenue_usd", "direction": "DESC"}], limit=1
    )
    ascending = {**gold, "order_by": [{"field": "revenue_usd", "direction": "ASC"}]}
    assert mcp_context.mismatched_slots(_case(gold, ordered=True), ascending, AGGREGATIONS) == [
        "order"
    ]
    assert mcp_context.mismatched_slots(_case(gold), ascending, AGGREGATIONS) == []


def test_alternatives_are_accepted() -> None:
    gold = _query(time=WINDOW_2017)
    yearly = _query(time={**WINDOW_2017, "grain": "year"})
    assert mcp_context.mismatched_slots(_case(gold), yearly, AGGREGATIONS) == ["grain"]
    assert (
        mcp_context.mismatched_slots(_case(gold, alternatives=[yearly]), yearly, AGGREGATIONS) == []
    )


def test_refusals() -> None:
    refuse = {"id": "T2", "category": "out_of_scope", "expect": "refuse"}
    answered = mcp_context.score_plan_response(refuse, _plan(_query()), AGGREGATIONS)
    assert answered.outcome == mcp_context.SILENT
    refused = mcp_context.score_plan_response(
        refuse, _plan(None, status="out_of_scope"), AGGREGATIONS
    )
    assert refused.outcome == mcp_context.PASS
    # An answerable question that the planner refuses is wrong, though loud.
    false_refusal = mcp_context.score_plan_response(
        _case(_query()), _plan(None, status="out_of_scope"), AGGREGATIONS
    )
    assert (false_refusal.outcome, false_refusal.mismatched) == (mcp_context.FLAGGED, ("refused",))


def test_plan_regressions_are_per_case() -> None:
    def outcome(case_id: str, result: str) -> mcp_context.PlanOutcome:
        return mcp_context.PlanOutcome(case_id, "test", result, "ok", (), ())

    baseline = {"cases": {"A": "pass", "B": "wrong_flagged", "C": "wrong_silent", "D": "pass"}}
    regressions, improvements = mcp_context.plan_regressions(
        [outcome("A", "pass"), outcome("B", "wrong_silent"), outcome("C", "pass")], baseline
    )
    assert [item.split(":")[0] for item in regressions] == ["B", "D"]
    assert [item.split(":")[0] for item in improvements] == ["C"]


def test_budget_check() -> None:
    budgets = {
        "tolerance": 0.02,
        "gated": {"q.a": 1000, "q.b": 1000, "q.c": 1000, "q.gone": 5},
        "tracked": {"q.t": 1},
    }
    metrics = {"q.a": 1020, "q.b": 1021, "q.c": 900, "q.new": 3, "q.t": 99}
    status = {
        check.metric: check.status
        for check in mcp_context.check_budgets(metrics, budgets, prefix="q.")
    }
    assert status == {
        "q.a": "ok",
        "q.b": "over",
        "q.c": "under",
        "q.gone": "unmeasured",
        "q.new": "unbudgeted",
        "q.t": "tracked",
    }
    failed = {
        check.metric
        for check in mcp_context.check_budgets(metrics, budgets, prefix="q.")
        if check.failed
    }
    assert failed == {"q.b", "q.gone", "q.new"}


FOOD = "measure.jaffle.food_revenue_usd"
DRINK = "measure.jaffle.drink_revenue_usd"
FOOD_AND_DRINK = {
    "version": 2,
    "select": [
        {"as": "food", "expression": {"measure": FOOD}},
        {"as": "drink", "expression": {"measure": DRINK}},
    ],
    "time": {"temporal_role": ORDER_TIME, "grain": "quarter"},
}
MEASURES = {FOOD: "sum", DRINK: "sum", REVENUE: "sum"}


def _table(rows: list[dict[str, Any]], query: dict[str, Any], *, trend: bool = True) -> Any:
    return mcp_context.answer_table(rows, query, MEASURES, trend=trend)


def test_answers_keep_each_value_in_its_column() -> None:
    gold = _table(
        [{"temporal_role.t__quarter": "2016-07-01 00:00:00", "food": 7971.0, "drink": 9061.0}],
        FOOD_AND_DRINK,
    )
    assert gold["columns"] == [f"{DRINK}:sum", f"{FOOD}:sum", "time"]
    swapped = _table(
        [{"temporal_role.t__quarter": "2016-07-01", "food": 9061.0, "drink": 7971.0}],
        FOOD_AND_DRINK,
    )
    assert not mcp_context.answers_match(gold, swapped, ordered=False)
    # Aliases, column order and float noise don't matter.
    renamed = {
        **FOOD_AND_DRINK,
        "select": [
            {"as": "d", "expression": {"measure": DRINK, "aggregation": "sum"}},
            {"as": "f", "expression": {"kind": "aggregate", "measure": FOOD}},
        ],
    }
    noisy = _table(
        [{"d": 9061.000000000186, "temporal_role.t__quarter": "2016-07-01", "f": 7970.99999999}],
        renamed,
    )
    assert mcp_context.answers_match(gold, noisy, ordered=False)
    changed = _table(
        [{"d": 9061.5, "temporal_role.t__quarter": "2016-07-01", "f": 7971.0}], renamed
    )
    assert not mcp_context.answers_match(gold, changed, ordered=False)


def test_one_value_computed_another_way_still_aligns() -> None:
    count = {
        "version": 2,
        "select": [{"as": "n", "expression": {"measure": "measure.jaffle.large_order_count"}}],
    }
    filtered = {
        "version": 2,
        "select": [{"as": "n", "expression": {"measure": "measure.jaffle.order_count"}}],
    }
    measures = {
        "measure.jaffle.large_order_count": "count",
        "measure.jaffle.order_count": "count_distinct",
    }
    gold = mcp_context.answer_table([{"n": 4303}], count, measures, trend=False)
    alternative = mcp_context.answer_table([{"n": 4303}], filtered, measures, trend=False)
    assert mcp_context.answers_match(gold, alternative, ordered=False)
    # With two value columns, a renamed one must still line up with the rest.
    assert mcp_context.answers_match(
        _table([{"food": 1.0, "drink": 2.0}], FOOD_AND_DRINK),
        _table(
            [{"food": 1.0, "revenue": 2.0}],
            {
                **FOOD_AND_DRINK,
                "select": [
                    {"as": "food", "expression": {"measure": FOOD}},
                    {"as": "revenue", "expression": {"measure": REVENUE}},
                ],
            },
        ),
        ordered=False,
    )


def test_answer_rows_compare_as_sets_unless_ranked() -> None:
    query = {
        "version": 2,
        "select": [{"as": "r", "expression": {"measure": REVENUE}}],
        "group_by": [STORE],
    }
    rows = [{STORE: "Brooklyn", "r": 1.0}, {STORE: "Philadelphia", "r": 2.0}]
    gold = _table(rows, query, trend=False)
    reversed_rows = _table(rows[::-1], query, trend=False)
    assert mcp_context.answers_match(gold, reversed_rows, ordered=False)
    assert not mcp_context.answers_match(gold, reversed_rows, ordered=True)
    # Without a trend, time buckets are not part of the answer.
    bucketed = _table(
        [{**row, "temporal_role.t__year": "2017-01-01"} for row in rows], query, trend=False
    )
    assert mcp_context.answers_match(gold, bucketed, ordered=True)


def test_failed_plan_calls_are_not_graded() -> None:
    refuse = {"id": "T3", "category": "out_of_scope", "expect": "refuse"}
    internal_error = {
        "ok": False,
        "status": "error",
        "errors": [{"code": "INTERNAL_ERROR"}],
        "warnings": [],
    }
    for response in ({}, internal_error):
        with pytest.raises(mcp_context.EvaluationError):
            mcp_context.score_plan_response(refuse, response, AGGREGATIONS)
        with pytest.raises(mcp_context.EvaluationError):
            mcp_context.score_plan_response(_case(_query()), response, AGGREGATIONS)
    # An uncertain answer to an unanswerable question is not a refusal.
    uncertain = {**_plan(_query(), status="low_confidence"), "ok": True}
    assert mcp_context.score_plan_response(refuse, uncertain, AGGREGATIONS).outcome == (
        mcp_context.FLAGGED
    )


@pytest.mark.parametrize("failing", ["segment-preview", "execute"])
def test_a_failing_call_fails_the_measurement(
    jaffle_package: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    real_call = mcp_context.QueryMCPClient.call_tool

    def call_tool(self: Any, name: str, arguments: Any) -> dict[str, Any]:
        if name != failing:
            return real_call(self, name, arguments)
        envelope = {"ok": False, "status": "error", "errors": [{"code": "INTERNAL_ERROR"}]}
        return {"content": [], "structuredContent": envelope, "isError": True}

    monkeypatch.setattr(mcp_context.QueryMCPClient, "call_tool", call_tool)
    with pytest.raises(mcp_context.MeasurementError, match=failing):
        mcp_context.measure_query_mcp(jaffle_package)


def test_timing_is_normalized_in_both_channels() -> None:
    compact = mcp_context.compact_json({"ok": True, "timing_ms": 1234.5678})
    indented = json.dumps({"ok": True, "timing_ms": 3.2}, indent=2)
    assert mcp_context.normalize_volatile(compact) == '{"ok":true,"timing_ms":10.000}'
    assert '"timing_ms": 10.000' in mcp_context.normalize_volatile(indented)


def test_a_modified_heldout_copy_is_rejected(
    tmp_path: Path, dev_cases: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    # Any held-out file must match the committed digest; this one is a relabeled dev case.
    impostor = tmp_path / "heldout.jsonl"
    impostor.write_text(json.dumps({**dev_cases[0], "split": "heldout"}) + "\n", encoding="utf-8")
    assert mcp_context.main(["--eval-file", str(impostor)]) == 1
    assert '"matches_heldout_commitment": false' in capsys.readouterr().out
