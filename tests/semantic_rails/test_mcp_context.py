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
FROZEN_DEV_SET_SHA256 = "2efd15836f7f21354e66ca215bd35c0ba25382cd32c3fa4998192d8ae535eaa9"


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


def test_answer_matching_tolerates_float_noise_only() -> None:
    gold = [[24857.99, "2017-03-01", "Brooklyn"], [35420.2, "2017-04-01", "Brooklyn"]]
    noisy = [
        {
            "store": "Brooklyn",
            "temporal_role.t__month": "2017-04-01 00:00:00",
            "revenue": 35420.200000000186,
        },
        {
            "store": "Brooklyn",
            "temporal_role.t__month": "2017-03-01",
            "revenue": 24857.989999999976,
        },
    ]
    assert mcp_context.rows_match(
        gold, mcp_context.canonical_rows(noisy, trend=True), ordered=False
    )
    assert not mcp_context.rows_match(
        gold, mcp_context.canonical_rows(noisy, trend=True), ordered=True
    )
    changed = [{**noisy[0], "revenue": 35420.3}, noisy[1]]
    assert not mcp_context.rows_match(
        gold, mcp_context.canonical_rows(changed, trend=True), ordered=False
    )
    # Without a trend, time buckets are not part of the answer.
    assert mcp_context.canonical_rows(noisy[:1], trend=False) == [[35420.200000000186, "Brooklyn"]]


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
