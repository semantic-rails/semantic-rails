"""Functional tests for the public ``plan`` intent surface."""

from __future__ import annotations

import json
from dataclasses import replace

import duckdb
import pytest
import yaml

from scripts.benchmark_plan import _load_cases
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import TOOL_DEFINITIONS, SemanticLayerMCPAdapter
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.call import Call, Select, time_of
from semantic_rails.planner.intent_ir import compose_hints, parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.planner.plan_query import _validate_query
from semantic_rails.planner.plan_trace import _select_best_plan
from semantic_rails.runtime import Runtime
from tests.semantic_rails.planner_fixtures import jaffle_runtime
from tests.semantic_rails.result_helpers import assert_plan_held

REVENUE = "measure.jaffle.revenue_usd"
REVENUE_SUM = {"kind": "measure", "measure": REVENUE, "aggregation": "sum"}


runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def _reference(runtime: Runtime, sql: str) -> list[tuple]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchall()


def test_best_plan_corpus_carries_query_once(runtime) -> None:
    def occurrences(value, query):
        count = int(value == query)
        if isinstance(value, dict):
            count += sum(occurrences(child, query) for child in value.values())
        elif isinstance(value, list):
            count += sum(occurrences(child, query) for child in value)
        return count

    def dereference(value, payload):
        if isinstance(value, dict):
            if "$ref" in value:
                target = payload
                for key in value["$ref"].split("."):
                    target = target[int(key)] if isinstance(target, list) else target[key]
                return target
            return {key: dereference(child, payload) for key, child in value.items()}
        if isinstance(value, list):
            return [dereference(child, payload) for child in value]
        return value

    for case in _load_cases():
        payload = plan_payload(runtime, intent=case["intent"], detail="best")
        best = payload.get("best") or {}
        full = plan_payload(runtime, intent=case["intent"], detail="full")
        assert dereference(payload["intent_ir"], payload) == full["intent_ir"]
        if query := best.get("query"):
            assert occurrences(payload, query) == 1, case["intent"]
            resolved = [json.dumps(row, sort_keys=True) for row in best["resolved"]]
            assert len(resolved) == len(set(resolved)), case["intent"]
            for row in best["resolved"]:
                assert occurrences(payload, row) == 1, case["intent"]
        for gap in payload.get("why", {}).get("details", {}).get("gaps", []):
            if isinstance(gap.get("actual"), dict):
                expanded_gap = next(
                    row for row in full["why"]["details"]["gaps"] if row["kind"] == gap["kind"]
                )
                assert dereference(gap["actual"], payload) == expanded_gap["actual"]
        if (payload.get("why") or {}).get("code") == "PLAN_FALLBACK_SEMANTIC_DRIFT":
            details = payload["why"]["details"]
            assert "primary_slots" not in details
            expanded = full["why"]["details"]
            assert expanded["primary_slots"] == best["trace"]["intent_slots"]
            for reason, long_reason in zip(details["reasons"], expanded["reasons"], strict=True):
                for field in ("expected", "actual"):
                    value = payload
                    for key in reason[field].split("."):
                        value = value[key]
                    assert value == long_reason[field]


@pytest.mark.parametrize(
    ("intent", "expected_id"),
    [
        ("new customer orders over time", "measure.jaffle.new_customer_order_count"),
        (
            "session to order conversion rate (7d)",
            "metric.sales.session_to_order_conversion_rate_7d",
        ),
        ("repeat customer orders by store", "metric.sales.repeat_customer_orders"),
        ("high value customer orders by store", "metric.sales.high_value_customer_orders"),
        (
            "month over month revenue growth by month",
            "metric.sales.month_over_month_revenue_growth",
        ),
        # An explicit qualifier outranks the generic revenue.
        ("drink revenue by store", "measure.jaffle.drink_revenue_usd"),
        ("food revenue last quarter", "measure.jaffle.food_revenue_usd"),
        ("delivered revenue by month", "measure.jaffle.delivered_revenue_usd"),
    ],
)
def test_plan_prefers_the_governed_or_qualified_subject(runtime, intent, expected_id) -> None:
    payload = plan_payload(runtime, intent=intent)
    if intent == "high value customer orders by store":
        # Its predicate reads a balance whose own time scope plan can't prove.
        assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    else:
        assert payload["status"] == "ok"
    best = payload["best"]
    projected_ids = {
        expression if isinstance(expression, str) else expression["measure"]
        for item in best["query"].get("select", [])
        for expression in [item if isinstance(item, str) else item["expression"]]
    }
    assert expected_id in projected_ids
    assert expected_id in best["subject_ids_used"]
    assert expected_id in {row["id"] for row in best["resolved"]}


@pytest.mark.parametrize(
    ("intent", "status", "code"),
    [
        (
            "Which customer segments drove the most historical revenue?",
            "low_confidence",
            "PLAN_INTENT_COVERAGE_GAP",
        ),
        # Grounded only by a time grain or by the package namespace, which every id carries.
        ("refunds by month", "out_of_scope", "LOW_RELEVANCE"),
        ("by month", "out_of_scope", "LOW_RELEVANCE"),
        ("how do I cook a jaffle at home", "out_of_scope", "LOW_RELEVANCE"),
        # The namespace word beside a real measure word still plans.
        ("jaffle revenue", "ok", None),
    ],
)
def test_plan_status_for_a_question(runtime, intent, status, code) -> None:
    payload = plan_payload(runtime, intent=intent)
    assert payload["status"] == status, payload.get("why")
    assert (payload.get("why") or {}).get("code") == code
    assert ("execute" in payload.get("next", {}).get("ready_for", [])) is (status == "ok")
    if status == "out_of_scope":
        assert payload["best"] is None
    elif status == "ok":
        assert payload["best"]["query"]["select"]
    else:
        assert "ranking_unrealized" in [gap["kind"] for gap in payload["why"]["details"]["gaps"]]


def test_plan_returns_out_of_scope_for_nonsense_at_each_detail(runtime) -> None:
    payload = plan_payload(runtime, intent="compile the linux kernel")
    assert payload["status"] == "out_of_scope"
    assert payload["best"] is None
    assert payload["next"] == {}
    assert payload["intent_ir"]["intent"] == "compile the linux kernel"
    query = plan_payload(runtime, intent="compile the linux kernel", detail="query")
    assert query["status"] == "out_of_scope"
    assert query["best"] is None
    assert "why" in query
    assert "intent_ir" not in query
    assert "next" not in query


@pytest.mark.parametrize("intent", ["   ", None])
def test_plan_rejects_missing_or_non_string_intent(runtime, intent) -> None:
    with pytest.raises(SemanticLayerError) as exc_info:
        plan_payload(runtime, intent=intent)
    assert exc_info.value.code == "INVALID_QUERY"
    assert exc_info.value.details["path"] == "intent"


def test_plan_preserves_partial_query_state(runtime) -> None:
    partial = {
        "group_by": ["dimension.jaffle_store_name"],
        "where": [{"field": "dimension.jaffle_order_has_food_item", "op": "is", "value": True}],
    }
    payload = plan_payload(runtime, intent="revenue", partial_query=partial)
    query_ir = payload["best"]["query"]
    assert "dimension.jaffle_store_name" in query_ir["group_by"]
    assert partial["where"][0] in query_ir["where"]
    assert payload["status"] == "ok"


@pytest.mark.parametrize(
    ("partial", "path"),
    [
        ({"group_by": [["dimension.jaffle_store_name"]]}, "group_by[0]"),
        ({"group_by": [{"label": "store"}]}, "group_by[0]"),
        # A group_by item is the dimension id itself, never an object naming it.
        ({"group_by": [{"dimension": "dimension.jaffle_store_name"}]}, "group_by[0]"),
        ({"group_by": "dimension.jaffle_store_name"}, "group_by"),
        ({"select": {"measure": REVENUE}}, "select"),
    ],
)
def test_plan_rejects_a_partial_query_it_cannot_read(runtime, partial, path) -> None:
    response = SemanticLayerMCPAdapter(runtime).call_tool(
        "plan", {"intent": "revenue by store", "query": partial}
    )
    assert response["ok"] is False
    error = response["errors"][0]
    assert (error["code"], error["details"]["path"]) == ("INVALID_QUERY", path)
    assert error["recovery_hints"][0]["code"] == "FIX_QUERY_CALL"


def test_plan_keeps_the_callers_store_name_grouping(runtime) -> None:
    partial = {"group_by": ["dimension.jaffle_store_name"]}
    payload = plan_payload(runtime, intent="revenue by store", partial_query=partial)
    # The caller's Store name settles how the stores show: the draft adds no Store id.
    assert payload["status"] == "ok"
    assert payload["best"]["query"]["group_by"] == ["dimension.jaffle_store_name"]


@pytest.mark.parametrize(
    ("caller", "alias", "shown"),
    [
        ({"as": "rev", "expression": REVENUE}, "rev", None),
        # The caller's spelling stays, the default aggregation restated too.
        ({"as": "rev", "expression": REVENUE_SUM}, "rev", None),
        # Without an alias of its own, the caller's item takes the draft's.
        ({"expression": REVENUE}, "revenue_usd", REVENUE),
    ],
)
def test_plan_keeps_a_caller_select_item_once(runtime, caller, alias, shown) -> None:
    payload = plan_payload(
        runtime, intent="top 3 stores by revenue", partial_query={"select": [caller]}
    )
    query = payload["best"]["query"]
    rows = runtime.query(query)["rows"]
    assert payload["status"] == "ok"
    assert query["select"] == [shown or {**caller, "as": alias}]
    assert query["order_by"] == [{"field": alias, "direction": "desc"}]
    assert sorted(rows[0]) == ["dimension.jaffle_store_id", "dimension.jaffle_store_name", alias]


def test_plan_reads_a_seed_ordered_by_a_selected_id(runtime) -> None:
    """The seed's order_by names its select item by id, as execute accepts it."""
    seed = {
        "select": [REVENUE],
        "group_by": ["dimension.jaffle_store_name"],
        "order_by": [{"field": REVENUE, "direction": "desc"}],
        "limit": 2,
    }
    assert Call.from_call(seed).order_by[0].field == "revenue_usd"
    payload = plan_payload(runtime, intent="revenue by store", partial_query=seed)
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    assert query["order_by"][0] == {"field": "revenue_usd", "direction": "desc"}
    by_column = {**seed, "order_by": [{"field": "revenue_usd", "direction": "desc"}]}
    assert runtime.query(query)["rows"] == runtime.query(by_column)["rows"]


def test_plan_keeps_a_different_computation_beside_the_caller_one(runtime) -> None:
    average = {"as": "avg_rev", "expression": {**REVENUE_SUM, "aggregation": "avg"}}
    payload = plan_payload(runtime, intent="revenue by store", partial_query={"select": [average]})
    select = payload["best"]["query"]["select"]
    expressions = [row if isinstance(row, str) else row["expression"] for row in select]
    assert expressions == [average["expression"], REVENUE]


def test_plan_demotes_a_failed_validation_and_caps_its_errors(runtime, monkeypatch) -> None:
    real_validate = runtime.validate

    def _flood_errors(payload, **kwargs):
        report = dict(real_validate(payload, **kwargs))
        report["ok"] = False
        report["errors"] = [
            {"code": "FABRICATED_FOR_TEST" if i == 0 else f"E_{i:02d}", "message": f"error {i}"}
            for i in range(10)
        ]
        return report

    monkeypatch.setattr(runtime, "validate", _flood_errors)
    for detail in ("best", "query"):
        payload = plan_payload(runtime, intent="revenue by store", detail=detail)
        assert payload["status"] == "low_confidence"
        assert payload["best"]["validation_ok"] is False
        assert payload["why"]["code"] == "VALIDATION_FAILED"
        assert payload["why"]["errors"][0]["code"] == "FABRICATED_FOR_TEST"
        assert len(payload["why"]["errors"]) == 3
        assert payload["why"]["truncated"]["dropped"] == 7
    assert "next" not in payload
    assert "trace" not in payload["best"]


@pytest.mark.parametrize("detail", ["best", "query"])
def test_plan_compact_detail_skips_catalog_fallback_for_valid_primary(
    runtime, monkeypatch, detail: str
) -> None:
    calls = []
    fallback = plan_module.fallback_drafts

    def _unexpected_fallback(*args, **kwargs):
        calls.append(True)
        return fallback(*args, **kwargs)

    monkeypatch.setattr(plan_module, "fallback_drafts", _unexpected_fallback)
    payload = plan_payload(runtime, intent="top stores by revenue", detail=detail)
    assert payload["status"] == "ok"
    assert not calls
    assert payload["best"]["pattern"] == "package_example"


def test_plan_compact_detail_discovers_fallback_after_invalid_primary(runtime, monkeypatch) -> None:
    # The question names the month the parsed time slot carries: a ranking of store-months
    # ("top stores by revenue" with that slot) is held for its unasked grain.
    intent = "monthly revenue by store"
    composed = plan_module.compose(runtime, intent)
    assert composed.draft is not None
    # Keep the synthetic rescue draft aligned with every parsed intent slot;
    # this test isolates fallback timing rather than the semantic-drift gate.
    valid_fallback = replace(
        composed.draft,
        query=replace(composed.draft.query, time=time_of(composed.intent_ir.time)),
    )
    missing = Select("measure.jaffle.missing", "missing_expression")
    invalid_primary = replace(
        valid_fallback,
        query=replace(valid_fallback.query, select=(*valid_fallback.query.select, missing)),
    )
    events: list[str] = []
    real_planned_row = plan_module._planned_row

    def _compose(_runtime, _intent):
        return CompositionResult(
            intent_ir=composed.intent_ir, draft=invalid_primary, pattern="synthetic_primary"
        )

    def _fallback_drafts(*args, **kwargs):  # noqa: ARG001
        events.append("discover_fallback")
        return [(valid_fallback, "catalog_fallback")]

    def _tracking_planned_row(runtime, draft, pattern, *args):
        events.append(f"validate:{pattern}")
        return real_planned_row(runtime, draft, pattern, *args)

    monkeypatch.setattr(plan_module, "compose", _compose)
    monkeypatch.setattr(plan_module, "fallback_drafts", _fallback_drafts)
    monkeypatch.setattr(plan_module, "_planned_row", _tracking_planned_row)
    payload = plan_payload(runtime, intent=intent, detail="best")

    assert events == [
        "validate:synthetic_primary",
        "discover_fallback",
        "validate:catalog_fallback",
    ]
    assert payload["status"] == "ok"
    assert payload["best"]["pattern"] == "catalog_fallback"
    assert payload["best"]["trace"]["fallback"]["used"] is True


def test_plan_uses_planner_owned_catalog_fallback(runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, intent: CompositionResult(
            intent_ir=parse_intent(runtime, intent), draft=None
        ),
    )
    payload = plan_payload(runtime, intent="lifetime spend")
    assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    assert "stock_as_of_unrealized" in {gap["kind"] for gap in payload["why"]["details"]["gaps"]}
    assert payload["best"]["pattern"] == "catalog_fallback"
    assert "query" in payload["best"]


def test_plan_best_detail_rejects_grouping_drift_fallback(runtime) -> None:
    best = plan_payload(runtime, intent="orders by customer status", detail="best")
    query = plan_payload(runtime, intent="orders by customer status", detail="query")
    full = plan_payload(runtime, intent="orders by customer status", detail="full", limit=3)
    assert best["status"] == "low_confidence"
    assert best["why"]["code"] == "PLAN_FALLBACK_SEMANTIC_DRIFT"
    assert best["best"]["pattern"] == "metric_by_dimension_rollup"
    assert best["best"]["validation_ok"] is False
    assert "ready_for" not in best["next"]
    assert full["status"] == "low_confidence"
    fallback_alt = full["alternatives"][0]
    assert fallback_alt["trace"]["selected"]["group_by"] == fallback_alt["query"].get(
        "group_by", []
    )
    assert query["status"] == best["status"]
    assert query["why"]["code"] == best["why"]["code"]
    assert query["best"]["query"] == best["best"]["query"]
    assert "next" not in query
    assert "trace" not in query["best"]
    assert len(json.dumps(best, default=str, separators=(",", ":"))) < len(
        json.dumps(full, default=str, separators=(",", ":"))
    )


class _FakeIntentIR:
    def __init__(self, *, subject: str, grouping: str, qualification: str) -> None:
        self.subject = subject
        self.grouping = grouping
        self.qualification = qualification

    def to_dict(self) -> dict:
        return {
            "subjects": [{"id": self.subject, "label": self.subject, "object_type": "measure"}],
            "grouping": [{"id": self.grouping, "label": self.grouping, "object_type": "dimension"}],
            "qualification_token_groups": [[self.qualification]],
            "threshold": {"op": "gte", "value": 2},
            "time": {},
        }


def _planned(query: dict, pattern: str, ok: bool) -> dict:
    draft = RuntimeCompositionDraft(
        query=Call.from_call(query),
        resolved=[],
        rationale=[],
        interpreted_intent={"pattern": pattern},
    )
    errors = [] if ok else [{"code": "REWRITE_NOT_SUPPORTED"}]
    return {"draft": draft, "pattern": pattern, "validation": {"ok": ok, "errors": errors}, "blocked": False}  # fmt: skip


@pytest.mark.parametrize("keeps_subject", [False, True])
def test_a_valid_fallback_replaces_the_primary_only_when_it_keeps_the_subject(
    keeps_subject: bool,
) -> None:
    subject, grouping = "measure.bank.loan_balance", "dimension.bank_branch"
    predicate, entity = "measure.bank.account_count", "entity.bank_customer"
    qualification = {
        "entity": entity,
        "having": [{"field": predicate, "op": "gte", "value": 2}],
        "over": "all_time",
    }
    primary = _planned(
        {"select": [subject], "group_by": [grouping], "where": [qualification]},
        "qualified_metric_rollup",
        ok=False,
    )
    target = subject if keeps_subject else "metric.bank.high_value_customer_accounts"
    fallback_query = {"select": [{"as": "fallback", "expression": target}], "group_by": [grouping]}
    if keeps_subject:
        fallback_query["where"] = [qualification]
    fallback = _planned(fallback_query, "catalog_fallback", ok=True)
    intent_ir = _FakeIntentIR(subject=subject, grouping=grouping, qualification=predicate)

    best = _select_best_plan([primary, fallback], intent_ir=intent_ir)

    if keeps_subject:
        assert best is fallback
        assert "semantic_drift" not in fallback
        return
    assert best is primary
    assert fallback["semantic_drift"]["code"] == "PLAN_FALLBACK_SEMANTIC_DRIFT"
    kinds = {row["kind"] for row in fallback["semantic_drift"]["details"]["reasons"]}
    assert {"target_changed", "qualification_dropped"} <= kinds


def test_plan_asks_about_a_comparison_of_complete_periods(runtime) -> None:
    payload = plan_payload(runtime, intent="revenue month over month", now="2017-08-15")
    rows = runtime.query(payload["best"]["query"], now="2017-08-15")["rows"]
    # A side-by-side comparison ships after this release: plan asks, offering the series.
    assert payload["status"] == "needs_clarification"
    assert payload["why"]["code"] == "PERIOD_COMPARISON"
    assert payload["next"] == {"action": "clarify"}
    assert payload["best"]["query"] == {
        "select": [REVENUE],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
            "last": {"unit": "month", "value": 2},
        },
        "order_by": [{"field": "period", "direction": "asc"}],
    }
    reference = _reference(
        runtime,
        "SELECT DATE_TRUNC('month', ordered_at)::DATE, SUM(order_total_cents) / 100.0 "
        "FROM jaffle_order WHERE ordered_at >= DATE '2017-06-01' "
        "AND ordered_at < DATE '2017-08-01' GROUP BY 1 ORDER BY 1",
    )
    assert [row["period"] for row in rows] == [str(day) for day, _value in reference]
    assert [row["revenue_usd"] for row in rows] == pytest.approx(
        [value for _day, value in reference]
    )


def test_plan_holds_a_comparison_of_a_period_not_over_yet(runtime, monkeypatch) -> None:
    fallback_calls = 0
    real_fallback_drafts = plan_module.fallback_drafts

    def _tracking_fallback_drafts(*args, **kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        return real_fallback_drafts(*args, **kwargs)

    monkeypatch.setattr(plan_module, "fallback_drafts", _tracking_fallback_drafts)
    intent = "revenue this month vs last month"
    best = plan_payload(runtime, intent=intent, detail="best", now="2017-08-15")
    compact_fallback_calls = fallback_calls
    full = plan_payload(runtime, intent=intent, detail="full", limit=3, now="2017-08-15")
    # This month isn't over, so it can't be compared with last month: plan holds and offers
    # the last two complete months as one series. The blocked primary outranks the fallback.
    for payload in (best, full):
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PERIOD_COMPARISON_INCOMPLETE"
        assert payload["best"]["pattern"] == "inline_period_shift"
    assert REVENUE in best["best"]["subject_ids_used"]
    assert compact_fallback_calls == 0
    assert fallback_calls == 1
    assert any(row["pattern"] == "catalog_fallback" for row in full["alternatives"])
    [option] = full["why"]["details"]["options"]
    assert option["call"]["time"]["last"] == {"unit": "month", "value": 2}
    assert option["call"]["time"]["grain"] == "month"


@pytest.mark.parametrize("detail", ["full", "debug"])
def test_plan_expanded_detail_returns_alternatives_and_blocked(
    runtime, monkeypatch, detail: str
) -> None:
    monkeypatch.setattr(runtime, "_package_examples", [])  # generic planner alternatives
    payload = plan_payload(runtime, intent="top stores by revenue", detail=detail, limit=3)
    assert payload["status"] == "ok"
    assert "alternatives" in payload
    assert "blocked" in payload
    assert ("compose_hints" in payload) is (detail == "debug")


def test_parse_intent_remains_internal_debug_helper(runtime) -> None:
    intent_ir = parse_intent(runtime, "top 3 stores by revenue with at least 4 distinct customers")
    ir = intent_ir.to_dict()
    assert ir["is_top_intent"] is True
    assert ir["top_n"] == 3
    assert ir["threshold"]["op"] == "gte"
    assert ir["threshold"]["value"] == 4
    assert compose_hints(intent_ir)["entity"]["id"] == "entity.jaffle_store"


@pytest.mark.parametrize(
    "intent",
    [
        "top stores by revenue",
        "monthly orders from customers who made more than 10 purchases in that month",
        "what is the session-to-order conversion rate (7d)",
    ],
)
def test_plan_compact_details_stay_token_tight(runtime, intent: str) -> None:
    best = plan_payload(runtime, intent=intent, detail="best")
    full = plan_payload(runtime, intent=intent, detail="full", limit=3)
    query = plan_payload(runtime, intent=intent, detail="query")

    def size(payload):
        return len(json.dumps(payload, default=str, separators=(",", ":")))

    assert size(best) < size(full)
    assert size(best) < 6_500
    assert size(query) < 2_500
    assert query["best"]["query"] == best["best"]["query"]
    assert set(query["best"]) <= {
        "pattern",
        "query",
        "resolved",
        "validation_ok",
        "subject_ids_used",
    }
    for omitted in (
        "intent_ir",
        "next",
        "trace",
        "rationale",
        "interpreted_intent",
        "alternatives",
        "blocked",
        "compose_hints",
    ):
        assert omitted not in query
        assert omitted not in query["best"]


def test_plan_answers_a_lifetime_cohort_with_an_entity_filter(runtime) -> None:
    payload = plan_payload(
        runtime,
        intent="Show sales by store for customers who made at least 2 orders in their lifetime.",
    )
    rows = runtime.query(payload["best"]["query"])["rows"]
    # "in their lifetime" says over which rows each customer is counted: all of them.
    assert payload["status"] == "ok", payload.get("why")
    assert payload["best"]["pattern"] == "qualified_metric_rollup"
    assert payload["best"]["query"]["where"] == [
        {
            "entity": "entity.jaffle_customer",
            "having": [{"field": "measure.jaffle.lifetime_order_count", "op": "gte", "value": 2}],
            "over": "all_time",
        }
    ]
    reference = _reference(
        runtime,
        "SELECT s.store_name, SUM(o.order_total_cents) / 100.0 FROM jaffle_order o "
        "JOIN jaffle_store s USING (store_id) WHERE o.customer_id IN "
        "(SELECT customer_id FROM jaffle_customer GROUP BY customer_id "
        "HAVING SUM(lifetime_order_count) >= 2) GROUP BY 1",
    )
    actual = {row["dimension.jaffle_store_name"]: row["revenue_usd"] for row in rows}
    assert actual == pytest.approx(dict(reference))


def test_plan_keeps_target_and_single_reachable_value_filter(runtime) -> None:
    payload = plan_payload(
        runtime, intent="Show total revenue by store where customer type is repeat.", detail="best"
    )
    assert payload["status"] == "ok"
    query = payload["best"]["query"]
    assert query["select"] == [REVENUE]
    assert query["group_by"] == ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]
    assert query["where"] == [
        {"field": "dimension.jaffle_customer_type", "op": "is", "value": "repeat"}
    ]
    assert payload["best"]["trace"]["intent_slots"]["target"]


def test_plan_structural_precheck_short_circuits() -> None:
    class _NoCallRuntime:
        def validate(self, payload, **kwargs):  # noqa: ARG002
            raise AssertionError("structural precheck should have short-circuited")

    result = _validate_query(_NoCallRuntime(), Call(group_by=("dimension.jaffle_store_name",)))
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "STRUCTURAL_EMPTY_SELECT"


def test_mcp_registers_only_plan_intent_tool() -> None:
    names = {definition.name for definition in TOOL_DEFINITIONS}
    assert "plan" in names
    assert not {"formulate", "propose", "expand", "parse-intent"} & names


def test_plan_per_dimension_keeps_plain_measure(runtime) -> None:
    """ "revenue per store" is "revenue by store", not a ratio-metric cue —
    it must not silently swap in cumulative/windowed revenue metrics."""
    payload = plan_payload(runtime, intent="revenue per store")
    assert_plan_held(payload, "PLAN_FALLBACK_SEMANTIC_DRIFT")
    query = payload["best"]["query"]
    assert query["select"] == ["metric.sales.month_over_month_revenue_growth"]
    assert not query.get("group_by")


def test_plan_top_n_without_time_cue_ranks_whole_dimension(runtime) -> None:
    """ "top stores by revenue" ranks stores overall. The defaulted month
    grain would rank store-months; only an explicit time cue keeps a bucket."""
    ranked = plan_payload(runtime, intent="top stores by revenue")
    bucketed = plan_payload(runtime, intent="top stores by revenue by month")
    ranked_query = ranked["best"]["query"]
    assert ranked["status"] == "ok"
    assert ranked["next"] == {"ready_for": ["execute"]}
    assert ranked["best"]["pattern"] == "package_example"
    # The month splits the stores it ranks: plan asks which ranking is meant.
    assert_plan_held(bucketed, "PLAN_RANKING_PERIOD_AMBIGUOUS")
    assert "time" not in ranked_query
    assert ranked_query["limit"] == 5
    assert ranked_query["order_by"][0]["direction"] == "desc"
    # An explicit grain cue keeps the time bucket.
    assert bucketed["best"]["query"]["time"]["grain"] == "month"


@pytest.mark.parametrize(
    ("name", "code"),
    [
        # "by revenue" after "What are the top stores" reads as a grouping it lacks.
        ("top_stores_by_revenue", "PLAN_UNMATCHED_TERMS"),
        ("inventory_levels_by_store", "PLAN_INTENT_COVERAGE_GAP"),
        # A balance summed over each month is not the snapshot asked for; that hold comes
        # before asking over which rows the stores qualify.
        ("active_menu_snapshot_for_high_activity_stores", "PLAN_INTENT_COVERAGE_GAP"),
        ("adversarial_fake_ids_route_to_real_revenue", "PLAN_UNMATCHED_TERMS"),
    ],
)
def test_benchmark_store_questions_require_clarification(runtime, name, code) -> None:
    case = next(row for row in _load_cases() if row["name"] == name)
    assert case["expected"] == "clarify"
    assert case["forbid_ready_for_execute"] is True
    payload = plan_payload(runtime, intent=case["intent"])
    assert payload["status"] == case["expected_status"]
    assert payload["why"]["code"] == code, payload.get("why")
    assert "execute" not in payload["next"].get("ready_for", [])


def test_a_statistic_no_metric_defines_holds_with_its_yaml(runtime) -> None:
    """ "Median revenue" reads Revenue at a statistic no metric defines: plan holds, hands the
    metric's YAML to paste, and the call reading the measure at the median now."""
    payload = plan_payload(runtime, intent="median revenue by store")
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "DEFINE_METRIC"
    assert "execute" not in payload["next"].get("ready_for", [])
    details = payload["why"]["details"]
    [(name, metric)] = yaml.safe_load(details["yaml"])["metrics"].items()
    assert name == "median_revenue_usd"
    assert metric["expression"] == {
        "kind": "aggregate",
        "measure": REVENUE,
        "aggregation": "median",
    }
    rows = runtime.query(details["components"]["call"])["rows"]
    reference = _reference(
        runtime,
        "SELECT s.store_name, MEDIAN(o.order_total_cents / 100.0) FROM jaffle_order o "
        "JOIN jaffle_store s USING (store_id) GROUP BY 1 ORDER BY 1",
    )
    actual = sorted((row["dimension.jaffle_store_name"], row[name]) for row in rows)
    assert [store for store, _ in actual] == [store for store, _ in reference]
    assert [value for _, value in actual] == pytest.approx([value for _, value in reference])
    # A measure already read at the asked statistic needs no metric.
    stock = plan_payload(runtime, intent="latest lifetime spend by customer type")
    assert stock["why"]["code"] != "DEFINE_METRIC"


def test_every_store_but_one_is_the_stores_an_exclusion_keeps(runtime) -> None:
    """ "For every store except Brooklyn" names the stores kept, not a row per store: one
    monthly series of the other stores' orders, every month of the named year."""
    payload = plan_payload(runtime, intent="monthly orders for every store except Brooklyn in 2017")
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    assert "group_by" not in query
    assert query["where"] == [
        {"field": "dimension.jaffle_store_name", "op": "not", "value": "Brooklyn"}
    ]
    rows = runtime.query(query)["rows"]
    counts = dict(
        _reference(
            runtime,
            "SELECT CAST(DATE_TRUNC('month', o.ordered_at) AS DATE)::VARCHAR, "
            "COUNT(DISTINCT o.order_id) FROM jaffle_order o "
            "JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name IS DISTINCT FROM 'Brooklyn' "
            "AND o.ordered_at >= '2017-01-01' AND o.ordered_at < '2018-01-01' GROUP BY 1",
        )
    )
    [(last,)] = _reference(
        runtime,
        "SELECT CAST(DATE_TRUNC('month', MAX(ordered_at)) AS DATE)::VARCHAR FROM jaffle_order",
    )
    # Every month of 2017: NULL for a month after the last order.
    months = [f"2017-{month:02d}-01" for month in range(1, 13)]
    reference = [counts.get(month, 0) if month <= last else None for month in months]
    assert [row["period"] for row in rows] == months
    assert [row["order_count"] for row in rows] == reference


def test_every_date_plan_resolves_comes_from_the_request_clock(runtime) -> None:
    """A named month and a balance's day are read on the request's now, never the wall clock,
    and the assumptions name the dates the draft reads."""
    now = "2017-11-04T00:00:00Z"
    september = plan_payload(runtime, intent="revenue in September", now=now)
    assert september["status"] == "ok", september.get("why")
    time = september["best"]["query"]["time"]
    assert (time["start"], time["end"]) == ("2017-09-01", "2017-10-01")
    assert any("2017-09-01" in text for text in september["assumptions"])
    [row] = runtime.query(september["best"]["query"], now=now)["rows"]
    balance = plan_payload(runtime, intent="active menu count", now="2017-04-02T12:00:00Z")
    assert balance["status"] == "ok", balance.get("why")
    assert balance["best"]["query"]["time"]["last"] == {"unit": "day", "value": 1}
    [read] = balance["assumptions"]
    assert "2017-04-01" in read
    [day] = runtime.query(balance["best"]["query"], now="2017-04-02T12:00:00Z")["rows"]
    [(revenue,)] = _reference(
        runtime,
        "SELECT SUM(order_total_cents / 100.0) FROM jaffle_order "
        "WHERE ordered_at >= '2017-09-01' AND ordered_at < '2017-10-01'",
    )
    [(menu,)] = _reference(
        runtime,
        "SELECT SUM(active_menu_count) FROM jaffle_store_inventory_snapshot "
        "WHERE date_day = DATE '2017-04-01'",
    )
    assert row["revenue_usd"] == pytest.approx(revenue)
    assert day["period"] == "2017-04-01"
    assert day["active_menu_count_eop"] == menu


def test_a_week_against_the_week_before_compares_one_measure(runtime) -> None:
    """ "The week before" names a period, not a second subject: the draft reads revenue alone,
    and plan asks whether to read the two weeks as one series."""
    payload = plan_payload(
        runtime, intent="revenue last week vs the week before", now="2017-08-21T00:00:00Z"
    )
    assert payload["status"] == "needs_clarification", payload.get("why")
    assert payload["why"]["code"] == "PERIOD_COMPARISON"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["best"]["query"] == {
        "select": [REVENUE],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "week",
            "last": {"unit": "week", "value": 2},
        },
        "order_by": [{"field": "period", "direction": "asc"}],
    }
