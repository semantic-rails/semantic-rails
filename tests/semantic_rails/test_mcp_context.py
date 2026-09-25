"""Hermetic gates on the query MCP's context cost and the planner's accuracy.

``scripts/mcp_context.py`` does the measuring against a throwaway jaffle_shop
fixture. These tests fail when a measured size exceeds its budget, when a gold
case's planner outcome gets worse or it gets another slot wrong, when a gold
query's answer changes, or when the frozen eval set is edited. See "Measuring
context cost" in docs/MCP_INTERFACE.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import mcp_context


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
    assert mcp_context.eval_set_digest(dev_cases) == mcp_context.DEV_SET_SHA256
    assert _plan_baseline()["eval_set_sha256"] == mcp_context.DEV_SET_SHA256


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


def test_default_probes_call_every_tool(jaffle_package: Path) -> None:
    with mcp_context.QueryMCPClient(jaffle_package) as client:
        listed = {tool["name"] for tool in client.request("tools/list")["tools"]}
    assert {tool for _name, tool, _arguments in mcp_context.V2_PROBES} == listed


def test_architect_tool_list_is_tracked(tmp_path: Path) -> None:
    metrics = mcp_context.measure_architect_mcp(tmp_path)
    # Recorded for comparison, not gated: the budgets gate the query MCP.
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
# The frozen answer of the synthetic cases below; a drafted query "returns" it
# unless a test says otherwise.
ANSWER = {"columns": [f"{REVENUE}:sum"], "rows": [[1.0]]}


def _case(gold: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": "T1",
        "category": "test",
        "expect": "answer",
        "gold_query": gold,
        "gold_result": ANSWER,
        **extra,
    }


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


def _score(
    case: dict[str, Any], response: dict[str, Any], *, answer: Any = ANSWER
) -> mcp_context.PlanOutcome:
    return mcp_context.score_plan_response(
        case, response, AGGREGATIONS, answer_of=lambda _query: answer
    )


WINDOW_2017 = {
    "temporal_role": ORDER_TIME,
    "grain": "month",
    "start": "2017-01-01",
    "end": "2018-01-01",
}


def test_dropped_window_is_a_silent_wrong_answer_unless_flagged() -> None:
    case = _case(_query(time=WINDOW_2017))
    dropped = _query(time={"temporal_role": ORDER_TIME, "grain": "month"})
    silent = _score(case, _plan(dropped))
    assert (silent.outcome, silent.mismatched) == (mcp_context.SILENT, ("window",))
    # A warning alone signals doubt; a non-ok status is the stronger signal.
    assert _score(case, _plan(dropped, warnings=1)).outcome == mcp_context.WARNED
    low_confidence = _score(case, _plan(dropped, status="low_confidence", warnings=1))
    assert low_confidence.outcome == mcp_context.FLAGGED


def test_a_flagged_correct_draft_is_a_false_alarm() -> None:
    case = _case(_query(time=WINDOW_2017))
    right = _query(time=WINDOW_2017)
    assert _score(case, _plan(right)).outcome == mcp_context.PASS
    assert _score(case, _plan(right, warnings=1)).outcome == mcp_context.PASS_FLAGGED
    downgraded = _score(case, _plan(right, status="low_confidence"))
    assert downgraded.outcome == mcp_context.PASS_FLAGGED
    # A refusal of an unanswerable question passes whatever else it reports.
    refuse = {"id": "T4", "category": "out_of_scope", "expect": "refuse"}
    assert _score(refuse, _plan(None, status="out_of_scope", warnings=1)).outcome == (
        mcp_context.PASS
    )


def test_a_plan_passes_only_if_its_query_returns_the_frozen_answer() -> None:
    case = _case(_query(time=WINDOW_2017))
    draft = _plan(_query(time=WINDOW_2017))
    assert _score(case, draft).outcome == mcp_context.PASS
    # Matching slots aren't enough: the rows must match too.
    other_rows = {"columns": ANSWER["columns"], "rows": [[2.0]]}
    wrong_rows = _score(case, draft, answer=other_rows)
    assert (wrong_rows.outcome, wrong_rows.mismatched) == (mcp_context.SILENT, ("answer",))
    did_not_run = _score(case, draft, answer=None)
    assert (did_not_run.outcome, did_not_run.mismatched) == (mcp_context.SILENT, ("answer",))


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
    # "=" with a list compares the dimension against the list's text.
    equals_list = _query(
        group_by=[STORE],
        where=[{"field": STORE, "op": "=", "value": ["Philadelphia", "Brooklyn"]}],
    )
    assert mcp_context.mismatched_slots(_case(gold), equals_list, AGGREGATIONS) == ["where"]


@pytest.mark.parametrize(
    ("change", "slot"),
    [
        ({"time": {**WINDOW_2017, "fill": True}}, "fill"),
        ({"time": {**WINDOW_2017, "calendar_id": "fiscal"}}, "calendar_id"),
        ({"temporal_role_overrides": {REVENUE: ORDER_TIME}}, "temporal_role_overrides"),
        ({"path_policy": {"ask_if_ambiguous": False}}, "path_policy"),
    ],
)
def test_every_field_that_can_change_rows_is_a_slot(change: dict[str, Any], slot: str) -> None:
    gold = _query(time=WINDOW_2017)
    assert mcp_context.mismatched_slots(_case(gold), {**gold, **change}, AGGREGATIONS) == [slot]


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
        "time": {**WINDOW_2017, "end": "2018-01-01T00:00:00", "fill": False},
        "path_policy": {"preference": "fewest_hops", "ask_if_ambiguous": True},
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
    # A wrong draft is compared with the closest accepted query.
    yearly_2016 = _query(time={**WINDOW_2017, "grain": "year", "start": "2016-01-01"})
    assert mcp_context.mismatched_slots(
        _case(gold, alternatives=[yearly]), yearly_2016, AGGREGATIONS
    ) == ["window"]


def test_refusals() -> None:
    refuse = {"id": "T2", "category": "out_of_scope", "expect": "refuse"}
    answered = _score(refuse, _plan(_query()))
    assert answered.outcome == mcp_context.SILENT
    refused = _score(refuse, _plan(None, status="out_of_scope"))
    assert refused.outcome == mcp_context.PASS
    # An answerable question that the planner refuses is wrong, though loud.
    false_refusal = _score(_case(_query()), _plan(None, status="out_of_scope"))
    assert (false_refusal.outcome, false_refusal.mismatched) == (mcp_context.FLAGGED, ("refused",))


def test_plan_regressions_are_per_case_and_per_slot() -> None:
    def outcome(case_id: str, result: str, *mismatched: str) -> mcp_context.PlanOutcome:
        return mcp_context.PlanOutcome(case_id, "test", result, "ok", (), mismatched)

    def entry(result: str, *mismatched: str) -> dict[str, Any]:
        return {"outcome": result, "mismatched": list(mismatched)}

    baseline = {
        "cases": {
            "A": entry("pass"),
            "B": entry("wrong_flagged", "grain"),
            "C": entry("wrong_silent", "window"),
            "D": entry("pass"),
            "E": entry("wrong_silent", "window"),
            "F": entry("wrong_silent", "grain", "window"),
            "G": entry("wrong_flagged", "refused"),
            "H": entry("pass"),
            "I": entry("wrong_warned", "select"),
        }
    }
    regressions, improvements = mcp_context.plan_regressions(
        [
            outcome("A", "pass"),
            outcome("B", "wrong_silent", "grain"),
            outcome("C", "pass"),
            # D is no longer evaluated.
            outcome("E", "wrong_silent", "window", "group_by"),
            outcome("F", "wrong_silent", "window"),
            # A draft where there was a refusal gets no slot newly wrong.
            outcome("G", "wrong_flagged", "grain"),
            # A correct answer that starts getting flagged is a new false alarm.
            outcome("H", "pass_flagged"),
            # A warning-only catch that becomes a status downgrade is progress.
            outcome("I", "wrong_flagged", "select"),
        ],
        baseline,
    )
    assert [item.split(":")[0] for item in regressions] == ["B", "E", "H", "D"]
    assert [item.split(":")[0] for item in improvements] == ["C", "F", "G", "I"]


def test_plan_baseline_is_json_with_one_case_per_line() -> None:
    outcomes = [
        mcp_context.PlanOutcome("A", "test", "pass", "ok", (), ()),
        mcp_context.PlanOutcome("B", "test", "wrong_silent", "ok", (), ("grain",)),
    ]
    text = mcp_context.plan_baseline_json(outcomes, [{"id": "A"}, {"id": "B"}])
    assert json.loads(text)["cases"] == {
        "A": {"outcome": "pass", "mismatched": []},
        "B": {"outcome": "wrong_silent", "mismatched": ["grain"]},
    }
    assert '  "B": {"outcome": "wrong_silent", "mismatched": ["grain"]}' in text.splitlines()


def test_budget_check() -> None:
    budgets = {
        "tolerance": 0.02,
        "gated": {
            "q.a_tokens": 1000,
            "q.b_tokens": 1000,
            "q.c_tokens": 1000,
            "q.gone_tokens": 5,
            "q.tools": 13,
        },
        "tracked": {"q.t_tokens": 1},
    }
    metrics = {
        "q.a_tokens": 1020,
        "q.b_tokens": 1021,
        "q.c_tokens": 900,
        "q.new_tokens": 3,
        "q.t_tokens": 99,
        # Counts get no slack.
        "q.tools": 14,
    }
    status = {
        check.metric: check.status
        for check in mcp_context.check_budgets(metrics, budgets, prefix="q.")
    }
    assert status == {
        "q.a_tokens": "ok",
        "q.b_tokens": "over",
        "q.c_tokens": "under",
        "q.gone_tokens": "unmeasured",
        "q.new_tokens": "unbudgeted",
        "q.t_tokens": "tracked",
        "q.tools": "over",
    }
    failed = {
        check.metric
        for check in mcp_context.check_budgets(metrics, budgets, prefix="q.")
        if check.failed
    }
    assert failed == {"q.b_tokens", "q.gone_tokens", "q.new_tokens", "q.tools"}


def test_rebaselining_moves_only_real_changes() -> None:
    previous = {"q.a_tokens": 1000, "q.b_tokens": 1000, "q.tools": 13, "q.gone_tokens": 5}
    metrics = {"q.a_tokens": 1015, "q.b_tokens": 900, "q.tools": 14, "q.new_tokens": 3}
    assert mcp_context.rebaseline(metrics, previous, 0.02) == {
        "q.a_tokens": 1000,
        "q.b_tokens": 900,
        "q.new_tokens": 3,
        "q.tools": 14,
    }


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
        [{"d": 9061.000000000186, "temporal_role.t__quarter": "2016-07-01", "f": 7970.9999999999}],
        renamed,
    )
    assert mcp_context.answers_match(gold, noisy, ordered=False)
    changed = _table(
        [{"d": 9061.5, "temporal_role.t__quarter": "2016-07-01", "f": 7971.0}], renamed
    )
    assert not mcp_context.answers_match(gold, changed, ordered=False)


def test_frozen_answers_are_canonicalized_and_compared_tightly() -> None:
    columns = ["measure.jaffle.order_count:count_distinct", "time"]
    frozen = {"columns": columns, "rows": [[939, "2017-07-03 00:00:00"]]}
    assert mcp_context.answers_match(
        frozen, {"columns": columns, "rows": [[939.0, "2017-07-03"]]}, ordered=False
    )
    # Fifty cents on a total over $600K is a different answer.
    total = {"columns": [f"{REVENUE}:sum"], "rows": [[612345.67]]}
    assert not mcp_context.answers_match(
        total, {"columns": total["columns"], "rows": [[612346.17]]}, ordered=False
    )


def test_a_pinned_dimension_is_not_part_of_the_answer() -> None:
    pinned = {
        "version": 2,
        "select": [{"as": "r", "expression": {"measure": REVENUE}}],
        "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}],
    }
    gold = _table([{"r": 5.0}], pinned, trend=False)
    grouped = _table([{STORE: "Brooklyn", "r": 5.0}], {**pinned, "group_by": [STORE]}, trend=False)
    assert grouped == gold


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
            _score(refuse, response)
        with pytest.raises(mcp_context.EvaluationError):
            _score(_case(_query()), response)
    # An uncertain answer to an unanswerable question is not a refusal.
    uncertain = {**_plan(_query(), status="low_confidence"), "ok": True}
    assert _score(refuse, uncertain).outcome == mcp_context.FLAGGED


INTERNAL_ERROR = {"ok": False, "status": "error", "errors": [{"code": "INTERNAL_ERROR"}]}


@pytest.mark.parametrize(
    ("tool", "arguments", "payload", "probe"),
    [
        (
            "segment",
            {"segment_id": mcp_context.SEGMENT, "action": "preview"},
            INTERNAL_ERROR,
            "segment_preview",
        ),
        # A mistake must fail with its own code, not with some other error...
        (
            "inspect",
            {"object_id": "revenue"},
            {"ok": False, "errors": [{"code": "INVALID_QUERY"}]},
            "inspect_label_not_id",
        ),
        # ...and one scripted to succeed with a warning must report the warning.
        (
            "discover",
            {"term": "revenue"},
            {"ok": True, "warnings": []},
            "discover_unknown_argument",
        ),
    ],
)
def test_a_call_that_misbehaves_fails_the_measurement(
    jaffle_package: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any] | None,
    payload: dict[str, Any],
    probe: str,
) -> None:
    real_call = mcp_context.QueryMCPClient.call_tool

    def call_tool(self: Any, name: str, called_with: Any) -> dict[str, Any]:
        if name != tool or (arguments is not None and called_with != arguments):
            return real_call(self, name, called_with)
        return {"content": [], "structuredContent": payload, "isError": not payload["ok"]}

    monkeypatch.setattr(mcp_context.QueryMCPClient, "call_tool", call_tool)
    with pytest.raises(mcp_context.MeasurementError, match=probe):
        mcp_context.measure_query_mcp(jaffle_package)


def test_timing_is_normalized_in_both_channels() -> None:
    compact = mcp_context.compact_json({"ok": True, "timing_ms": 1234.5678, "compile_ms": 3})
    indented = json.dumps({"ok": True, "cache_lookup_ms": 3.2}, indent=2)
    assert mcp_context.normalize_volatile(compact) == (
        '{"compile_ms":10.000,"ok":true,"timing_ms":10.000}'
    )
    assert '"cache_lookup_ms": 10.000' in mcp_context.normalize_volatile(indented)


def test_eval_file_must_be_a_frozen_split(
    tmp_path: Path, dev_cases: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    # A copy that matches neither committed digest is rejected, whatever its labels say.
    impostor = tmp_path / "heldout.jsonl"
    impostor.write_text(json.dumps({**dev_cases[0], "split": "heldout"}) + "\n", encoding="utf-8")
    assert mcp_context.main(["--eval-file", str(impostor)]) == 1
    assert '"frozen_split": null' in capsys.readouterr().out
    assert mcp_context.main(["--eval-file", str(impostor), "--allow-unfrozen"]) == 0


def test_a_session_may_only_use_ids_it_was_shown(
    jaffle_package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Executing a query before anything surfaced its ids isn't a path an agent could take.
    blind = [("execute", "execute", {"query": mcp_context.Q1})]
    monkeypatch.setattr(mcp_context, "SESSIONS", {"blind": blind})
    with pytest.raises(mcp_context.MeasurementError, match="no earlier call in the session"):
        mcp_context.measure_query_mcp(jaffle_package)
