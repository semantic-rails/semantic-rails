"""Functional tests for the public ``plan`` intent surface."""

from __future__ import annotations

import json

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.intent_ir import compose_hints, parse_intent


def test_plan_returns_status_ok_for_realizable_intent(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime,
            intent="top 3 stores by revenue with at least 4 distinct customers",
        )
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert payload["best"] is not None
    assert payload["best"]["pattern"] == "qualified_metric_rollup"
    assert payload["best"]["validation_ok"] is True
    assert "metric_filters" in payload["best"]["query_ir"]


@pytest.mark.parametrize(
    ("intent", "expected_id"),
    [
        ("new customer orders over time", "measure.jaffle.new_customer_order_count"),
        ("session to order conversion rate", "metric.sales.session_to_order_conversion_rate_7d"),
        ("repeat customer orders by store", "metric.sales.repeat_customer_orders"),
        ("high value customer orders by store", "metric.sales.high_value_customer_orders"),
        (
            "month over month revenue growth by month",
            "metric.sales.month_over_month_revenue_growth",
        ),
    ],
)
def test_plan_prefers_governed_subject_over_generic_order_count(
    runtime_factory, intent: str, expected_id: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    best = payload["best"]
    query = best["query_ir"]
    projected_ids = {
        expression.get("metric") or expression.get("measure")
        for item in query.get("select", [])
        for expression in [item.get("expression", {})]
    }
    assert expected_id in projected_ids
    assert expected_id in {row["id"] for row in best["resolved"]}


@pytest.mark.parametrize(
    ("intent", "expected_id"),
    [
        ("drink revenue by store", "measure.jaffle.drink_revenue_usd"),
        ("food revenue last quarter", "measure.jaffle.food_revenue_usd"),
        ("delivered revenue by month", "measure.jaffle.delivered_revenue_usd"),
    ],
)
def test_plan_prefers_explicit_revenue_qualifier_over_generic_revenue(
    runtime_factory, intent: str, expected_id: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert expected_id in payload["best"]["subject_ids_used"]
    assert expected_id in {row["id"] for row in payload["best"]["resolved"]}


def test_plan_resolves_historical_customer_segment_grouping(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime, intent="Which customer segments drove the most historical revenue?"
        )
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert "dimension.jaffle_customer_history_segment" in payload["best"]["query_ir"]["group_by"]


@pytest.mark.parametrize("intent", ["", "   ", None, 123])
def test_plan_rejects_missing_or_non_string_intent(runtime_factory, intent) -> None:
    from semantic_rails.errors import SemanticLayerError

    runtime = runtime_factory("jaffle_shop")
    try:
        with pytest.raises(SemanticLayerError) as exc_info:
            plan_payload(runtime, intent=intent)
    finally:
        runtime.close()
    assert exc_info.value.code == "INVALID_QUERY"
    assert exc_info.value.details["path"] == "intent"


def test_plan_preserves_partial_query_state(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    partial = {
        "version": 1,
        "group_by": ["dimension.jaffle_store_name"],
        "where": [
            {
                "field": "dimension.jaffle_order_has_food_item",
                "op": "=",
                "value": True,
            }
        ],
    }
    try:
        payload = plan_payload(runtime, intent="revenue", partial_query=partial)
    finally:
        runtime.close()
    query_ir = payload["best"]["query_ir"]
    assert "dimension.jaffle_store_name" in query_ir["group_by"]
    assert partial["where"][0] in query_ir["where"]
    assert payload["status"] == "ok"


def test_plan_demotes_to_low_confidence_on_validation_failure(runtime_factory, monkeypatch) -> None:
    runtime = runtime_factory("jaffle_shop")
    real_validate = runtime.validate

    def _failing_validate(payload):
        report = dict(real_validate(payload))
        report["ok"] = False
        report["errors"] = [
            {"code": "FABRICATED_FOR_TEST", "message": "synthetic validation failure"}
        ]
        return report

    monkeypatch.setattr(runtime, "validate", _failing_validate)
    try:
        payload = plan_payload(
            runtime,
            intent="top 3 stores by revenue with at least 4 distinct customers",
        )
    finally:
        runtime.close()
    assert payload["status"] == "low_confidence"
    assert payload["best"]["validation_ok"] is False
    assert payload["why"]["code"] == "VALIDATION_FAILED"
    assert any(err.get("code") == "FABRICATED_FOR_TEST" for err in payload["why"]["errors"])


def test_plan_caps_why_errors_with_truncation_marker(runtime_factory, monkeypatch) -> None:
    runtime = runtime_factory("jaffle_shop")
    real_validate = runtime.validate

    def _flood_errors(payload):
        report = dict(real_validate(payload))
        report["ok"] = False
        report["errors"] = [
            {"code": f"E_{i:02d}", "message": f"synthetic error {i}"} for i in range(10)
        ]
        return report

    monkeypatch.setattr(runtime, "validate", _flood_errors)
    try:
        payload = plan_payload(
            runtime,
            intent="top 3 stores by revenue with at least 4 distinct customers",
        )
    finally:
        runtime.close()
    assert len(payload["why"]["errors"]) == 3
    assert payload["why"]["truncated"]["dropped"] == 7


def test_plan_next_signals_ready_for_compile_and_execute(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="top stores by revenue")
    finally:
        runtime.close()
    next_block = payload["next"]
    assert next_block["ready_for"] == ["compile", "execute"]
    assert next_block["validate"]["query"] == payload["best"]["query_ir"]
    assert "compile" not in next_block
    assert "execute" not in next_block


@pytest.mark.parametrize("detail", ["best", "query"])
def test_plan_compact_detail_skips_catalog_fallback_for_valid_primary(
    runtime_factory, monkeypatch, detail: str
) -> None:
    from semantic_rails.planner import plan as plan_module

    runtime = runtime_factory("jaffle_shop")

    def _unexpected_fallback(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("valid compact plans must not discover catalog fallbacks")

    monkeypatch.setattr(plan_module, "fallback_drafts", _unexpected_fallback)
    try:
        payload = plan_payload(runtime, intent="top stores by revenue", detail=detail)
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert payload["best"]["pattern"] == "metric_by_dimension_rollup"


def test_plan_compact_detail_discovers_fallback_after_invalid_primary(
    runtime_factory, monkeypatch
) -> None:
    from dataclasses import replace

    from semantic_rails.planner import plan as plan_module
    from semantic_rails.planner.orchestrator import CompositionResult

    runtime = runtime_factory("jaffle_shop")
    intent = "top stores by revenue"
    composed = plan_module.compose(runtime, intent)
    assert composed.draft is not None
    # Keep the synthetic rescue draft aligned with every parsed intent slot;
    # this test isolates fallback timing rather than the semantic-drift gate.
    valid_fallback = replace(
        composed.draft,
        query={**composed.draft.query, "time": dict(composed.intent_ir.time or {})},
    )
    invalid_primary = replace(
        valid_fallback,
        query={
            **valid_fallback.query,
            "select": [*valid_fallback.query["select"], {"as": "missing_expression"}],
        },
    )
    events: list[str] = []
    real_planned_row = plan_module._planned_row

    def _compose(_runtime, _intent):
        return CompositionResult(
            intent_ir=composed.intent_ir,
            draft=invalid_primary,
            pattern="synthetic_primary",
        )

    def _fallback_drafts(*args, **kwargs):  # noqa: ARG001
        events.append("discover_fallback")
        return [(valid_fallback, "catalog_fallback")]

    def _tracking_planned_row(runtime, draft, pattern, partial_query, blocked):
        events.append(f"validate:{pattern}")
        return real_planned_row(runtime, draft, pattern, partial_query, blocked)

    monkeypatch.setattr(plan_module, "compose", _compose)
    monkeypatch.setattr(plan_module, "fallback_drafts", _fallback_drafts)
    monkeypatch.setattr(plan_module, "_planned_row", _tracking_planned_row)
    try:
        payload = plan_payload(runtime, intent=intent, detail="best")
    finally:
        runtime.close()

    assert events == [
        "validate:synthetic_primary",
        "discover_fallback",
        "validate:catalog_fallback",
    ]
    assert payload["status"] == "ok"
    assert payload["best"]["pattern"] == "catalog_fallback"
    assert payload["best"]["trace"]["fallback"]["used"] is True


def test_plan_uses_planner_owned_catalog_fallback(runtime_factory, monkeypatch) -> None:
    from semantic_rails.planner import plan as plan_module
    from semantic_rails.planner.orchestrator import CompositionResult

    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, intent: CompositionResult(
            intent_ir=parse_intent(runtime, intent),
            draft=None,
        ),
    )
    try:
        payload = plan_payload(runtime, intent="lifetime spend")
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert payload["best"] is not None
    assert payload["best"]["pattern"] == "catalog_fallback"
    assert "query_ir" in payload["best"]


def test_plan_best_detail_rejects_grouping_drift_fallback(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        best = plan_payload(runtime, intent="orders by customer status", detail="best")
        query = plan_payload(runtime, intent="orders by customer status", detail="query")
        full = plan_payload(runtime, intent="orders by customer status", detail="full", limit=3)
    finally:
        runtime.close()
    assert best["status"] == "low_confidence"
    assert best["why"]["code"] == "PLAN_FALLBACK_SEMANTIC_DRIFT"
    assert best["best"]["pattern"] == "metric_by_dimension_rollup"
    assert best["best"]["validation_ok"] is False
    assert "ready_for" not in best["next"]
    assert full["status"] == "low_confidence"
    fallback_alt = full["alternatives"][0]
    assert fallback_alt["trace"]["selected"]["group_by"] == fallback_alt["query_ir"].get(
        "group_by", []
    )
    assert query["status"] == best["status"]
    assert query["why"]["code"] == best["why"]["code"]
    assert query["best"]["query_ir"] == best["best"]["query_ir"]
    assert "next" not in query
    assert "trace" not in query["best"]
    assert len(json.dumps(best, default=str, separators=(",", ":"))) < len(
        json.dumps(full, default=str, separators=(",", ":"))
    )


class _FakeIntentIR:
    def __init__(self, *, subject: str, grouping: str, qualification: str = "cohort") -> None:
        self.subject = subject
        self.grouping = grouping
        self.qualification = qualification

    def to_dict(self) -> dict:
        return {
            "subjects": [{"id": self.subject, "label": self.subject, "object_type": "measure"}],
            "grouping": [{"id": self.grouping, "label": self.grouping, "object_type": "dimension"}],
            "qualification_token_groups": [[self.qualification]],
            "threshold": {"op": ">=", "value": 2},
            "time": {},
        }


def _qualified_primary(*, target: str, predicate: str, entity: str, group_by: str):
    return {
        "draft": RuntimeCompositionDraft(
            query={
                "version": 2,
                "select": [
                    {
                        "as": "qualified_target",
                        "expression": {
                            "kind": "scoped_aggregate",
                            "aggregation": "sum",
                            "measure": target,
                            "predicates": [
                                {
                                    "entity": entity,
                                    "measure": predicate,
                                    "op": ">=",
                                    "value": 2,
                                }
                            ],
                        },
                    }
                ],
                "group_by": [group_by],
                "metric_filters": [
                    {
                        "expression": {
                            "kind": "metric_predicate",
                            "entity": entity,
                            "input": {"measure": predicate},
                            "op": ">=",
                            "value": 2,
                        },
                        "op": "=",
                        "value": True,
                    }
                ],
            },
            resolved=[],
            rationale=[],
            interpreted_intent={"pattern": "qualified_metric_rollup"},
        ),
        "pattern": "qualified_metric_rollup",
        "validation": {"ok": False, "errors": [{"code": "REWRITE_NOT_SUPPORTED"}]},
        "blocked": False,
    }


def _fallback_row(*, target: str, group_by: str, metric_filters: list[dict] | None = None):
    query = {
        "version": 1,
        "select": [{"as": "fallback", "expression": {"metric": target}}],
        "group_by": [group_by],
    }
    if metric_filters:
        query["metric_filters"] = metric_filters
    return {
        "draft": RuntimeCompositionDraft(
            query=query,
            resolved=[],
            rationale=[],
            interpreted_intent={"pattern": "catalog_fallback"},
        ),
        "pattern": "catalog_fallback",
        "validation": {"ok": True, "errors": []},
        "blocked": False,
    }


@pytest.mark.parametrize(
    ("subject", "grouping", "predicate", "entity", "bad_fallback"),
    [
        (
            "measure.bank.loan_balance",
            "dimension.bank_branch",
            "measure.bank.account_count",
            "entity.bank_customer",
            "metric.bank.high_value_customer_accounts",
        ),
        (
            "measure.pharmacy.dispensed_units",
            "dimension.pharmacy_location",
            "measure.pharmacy.fill_count",
            "entity.pharmacy_patient",
            "metric.pharmacy.adherent_patient_count",
        ),
    ],
)
def test_plan_rejects_domain_agnostic_semantic_drift_fallback(
    subject: str, grouping: str, predicate: str, entity: str, bad_fallback: str
) -> None:
    from semantic_rails.planner.plan import _select_best_plan

    primary = _qualified_primary(
        target=subject, predicate=predicate, entity=entity, group_by=grouping
    )
    fallback = _fallback_row(target=bad_fallback, group_by=grouping)
    planned = [primary, fallback]

    best = _select_best_plan(
        planned,
        intent_ir=_FakeIntentIR(subject=subject, grouping=grouping, qualification=predicate),
    )

    assert best is primary
    assert fallback["semantic_drift"]["code"] == "PLAN_FALLBACK_SEMANTIC_DRIFT"
    kinds = {row["kind"] for row in fallback["semantic_drift"]["details"]["reasons"]}
    assert "target_changed" in kinds
    assert "qualification_dropped" in kinds


def test_plan_allows_subject_preserving_valid_fallback() -> None:
    from semantic_rails.planner.plan import _select_best_plan

    subject = "measure.bank.loan_balance"
    grouping = "dimension.bank_branch"
    predicate = "measure.bank.account_count"
    entity = "entity.bank_customer"
    primary = _qualified_primary(
        target=subject, predicate=predicate, entity=entity, group_by=grouping
    )
    fallback = _fallback_row(
        target=subject,
        group_by=grouping,
        metric_filters=primary["draft"].query["metric_filters"],
    )

    best = _select_best_plan(
        [primary, fallback],
        intent_ir=_FakeIntentIR(subject=subject, grouping=grouping, qualification=predicate),
    )

    assert best is fallback
    assert "semantic_drift" not in fallback


def test_plan_preserves_period_shift_intent_instead_of_validating_generic_fallback(
    runtime_factory,
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue this month vs last month", detail="full")
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert payload["best"]["pattern"] == "inline_period_shift"
    assert payload["best"]["validation_ok"] is True
    query = payload["best"]["query_ir"]
    assert "start" not in query["time"]
    assert any(
        (item.get("expression") or {}).get("kind") == "prior_period" for item in query["select"]
    )


def test_plan_preserves_explicitly_blocked_primary_over_generic_fallback(
    runtime_factory, monkeypatch
) -> None:
    from semantic_rails.planner import plan as plan_module

    runtime = runtime_factory("jaffle_shop")
    fallback_calls = 0
    real_fallback_drafts = plan_module.fallback_drafts

    def _tracking_fallback_drafts(*args, **kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        return real_fallback_drafts(*args, **kwargs)

    monkeypatch.setattr(plan_module, "fallback_drafts", _tracking_fallback_drafts)
    try:
        best = plan_payload(
            runtime,
            intent="Compare delivered revenue by delivered month versus ordered month.",
            detail="best",
        )
        compact_fallback_calls = fallback_calls
        full = plan_payload(
            runtime,
            intent="Compare delivered revenue by delivered month versus ordered month.",
            detail="full",
            limit=3,
        )
    finally:
        runtime.close()
    assert best["status"] == "low_confidence"
    assert best["best"]["pattern"] == "inline_comparison"
    assert best["why"]["errors"][0]["code"] == "COMPARISON_COORDINATED"
    assert "measure.jaffle.delivered_revenue_usd" in best["best"]["subject_ids_used"]
    assert "measure.jaffle.revenue_usd" in best["best"]["subject_ids_used"]
    assert compact_fallback_calls == 0
    assert fallback_calls == 1
    assert full["status"] == "low_confidence"
    assert full["best"]["pattern"] == "inline_comparison"
    assert any(row["pattern"] == "catalog_fallback" for row in full["alternatives"])


def test_plan_returns_out_of_scope_for_nonsense(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="compile the linux kernel")
    finally:
        runtime.close()
    assert payload["status"] == "out_of_scope"
    assert payload["best"] is None
    assert payload["next"] == {}
    assert payload["intent_ir"]["intent"] == "compile the linux kernel"


@pytest.mark.parametrize("detail", ["full", "debug"])
def test_plan_expanded_detail_returns_alternatives_and_blocked(
    runtime_factory, detail: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="top stores by revenue", detail=detail, limit=3)
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    assert "alternatives" in payload
    assert "blocked" in payload
    assert ("compose_hints" in payload) is (detail == "debug")


def test_parse_intent_remains_internal_debug_helper(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        intent_ir = parse_intent(
            runtime, "top 3 stores by revenue with at least 4 distinct customers"
        )
    finally:
        runtime.close()
    ir = intent_ir.to_dict()
    assert ir["is_top_intent"] is True
    assert ir["top_n"] == 3
    assert ir["threshold"]["op"] == ">="
    assert ir["threshold"]["value"] == 4
    assert compose_hints(intent_ir)["entity"]["id"] == "entity.jaffle_store"


@pytest.mark.parametrize(
    "intent",
    [
        "top stores by revenue",
        "monthly orders from customers who made more than 10 purchases in that month",
        "what is the session-to-order conversion rate",
    ],
)
def test_plan_payload_stays_token_tight(runtime_factory, intent: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        best = plan_payload(runtime, intent=intent, detail="best")
        full = plan_payload(runtime, intent=intent, detail="full", limit=3)
    finally:
        runtime.close()
    best_size = len(json.dumps(best, default=str, separators=(",", ":")))
    full_size = len(json.dumps(full, default=str, separators=(",", ":")))
    assert best_size < full_size
    assert best_size < 6_500


@pytest.mark.parametrize(
    "intent",
    [
        "top stores by revenue",
        "monthly orders from customers who made more than 10 purchases in that month",
        "what is the session-to-order conversion rate",
    ],
)
def test_plan_query_detail_matches_best_query_ir_and_stays_compact(
    runtime_factory, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        query = plan_payload(runtime, intent=intent, detail="query")
        best = plan_payload(runtime, intent=intent, detail="best")
    finally:
        runtime.close()

    assert query["best"]["query_ir"] == best["best"]["query_ir"]
    assert set(query["best"]) <= {
        "pattern",
        "query_ir",
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
    assert len(json.dumps(query, default=str, separators=(",", ":"))) < 2_500


def test_plan_query_detail_preserves_out_of_scope_shape(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="compile the linux kernel", detail="query")
    finally:
        runtime.close()

    assert payload["status"] == "out_of_scope"
    assert payload["best"] is None
    assert "why" in payload
    assert "intent_ir" not in payload
    assert "next" not in payload


def test_plan_query_detail_preserves_low_confidence_validation_failure(
    runtime_factory, monkeypatch
) -> None:
    runtime = runtime_factory("jaffle_shop")
    real_validate = runtime.validate

    def _failing_validate(payload):
        report = dict(real_validate(payload))
        report["ok"] = False
        report["errors"] = [
            {"code": "FABRICATED_FOR_TEST", "message": "synthetic validation failure"}
        ]
        return report

    monkeypatch.setattr(runtime, "validate", _failing_validate)
    try:
        payload = plan_payload(
            runtime,
            intent="top 3 stores by revenue with at least 4 distinct customers",
            detail="query",
        )
    finally:
        runtime.close()

    assert payload["status"] == "low_confidence"
    assert payload["best"]["validation_ok"] is False
    assert payload["why"]["code"] == "VALIDATION_FAILED"
    assert any(err.get("code") == "FABRICATED_FOR_TEST" for err in payload["why"]["errors"])
    assert "next" not in payload
    assert "trace" not in payload["best"]


def test_plan_does_not_promote_target_changing_lifetime_cohort_fallback(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime,
            intent="Show sales by store for customers who made at least 2 orders in their lifetime.",
            detail="full",
            limit=3,
        )
    finally:
        runtime.close()

    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_FALLBACK_SEMANTIC_DRIFT"
    assert payload["best"]["pattern"] == "qualified_metric_rollup"
    assert "metric.sales.high_value_customer_orders" not in payload["best"]["subject_ids_used"]


def test_plan_keeps_target_and_single_reachable_value_filter(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime,
            intent="Show total revenue by store where customer type is repeat.",
            detail="best",
        )
    finally:
        runtime.close()

    assert payload["status"] == "ok"
    query = payload["best"]["query_ir"]
    assert query["select"] == [
        {
            "as": "revenue_usd",
            "expression": {"measure": "measure.jaffle.revenue_usd", "aggregation": "sum"},
        }
    ]
    assert query["group_by"] == ["dimension.jaffle_store_name"]
    assert query["where"] == [
        {"field": "dimension.jaffle_customer_type", "op": "=", "value": "repeat"}
    ]
    assert payload["best"]["trace"]["intent_slots"]["target"]


def test_plan_structural_precheck_catches_empty_select() -> None:
    from semantic_rails.planner.plan import _validate_query

    class _NoCallRuntime:
        def validate(self, payload):  # noqa: ARG002
            raise AssertionError("structural precheck should have short-circuited")

    result = _validate_query(_NoCallRuntime(), {"select": []}, None)
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "STRUCTURAL_EMPTY_SELECT"


def test_plan_structural_precheck_catches_missing_expression() -> None:
    from semantic_rails.planner.plan import _validate_query

    class _NoCallRuntime:
        def validate(self, payload):  # noqa: ARG002
            raise AssertionError("structural precheck should have short-circuited")

    result = _validate_query(_NoCallRuntime(), {"select": [{"as": "x"}]}, None)
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "STRUCTURAL_MISSING_EXPRESSION"


def test_mcp_registers_only_plan_intent_tool() -> None:
    from semantic_rails.mcp import TOOL_DEFINITIONS

    names = {definition.name for definition in TOOL_DEFINITIONS}
    assert "plan" in names
    assert not {"formulate", "propose", "expand", "parse-intent"} & names


def test_plan_refuses_intents_grounded_only_by_time_grain_tokens(runtime_factory) -> None:
    """ "refunds by month" overlaps the catalog only through "month" — the
    grounding floor must refuse instead of confidently serving an unrelated
    measure that happens to share the grain token."""
    runtime = runtime_factory("jaffle_shop")
    try:
        refunds = plan_payload(runtime, intent="refunds by month")
        bare_grain = plan_payload(runtime, intent="by month")
    finally:
        runtime.close()
    for payload in (refunds, bare_grain):
        assert payload["status"] == "out_of_scope"
        assert payload["best"] is None
        assert payload["why"]["code"] == "LOW_RELEVANCE"


def test_plan_refuses_intents_grounded_only_by_package_namespace(runtime_factory) -> None:
    """The package namespace token ("jaffle") appears in every object id, so
    it alone must not ground an off-topic question."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="how do I cook a jaffle at home")
        grounded = plan_payload(runtime, intent="jaffle revenue")
    finally:
        runtime.close()
    assert payload["status"] == "out_of_scope"
    assert payload["why"]["code"] == "LOW_RELEVANCE"
    # The namespace token plus a real measure word still plans normally.
    assert grounded["status"] == "ok"
    assert grounded["best"]["query_ir"]["select"]


def test_plan_per_dimension_keeps_plain_measure(runtime_factory) -> None:
    """ "revenue per store" is "revenue by store", not a ratio-metric cue —
    it must not silently swap in cumulative/windowed revenue metrics."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue per store")
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    query = payload["best"]["query_ir"]
    expressions = [row["expression"] for row in query["select"]]
    assert expressions == [{"measure": "measure.jaffle.revenue_usd", "aggregation": "sum"}]
    assert query["group_by"] == ["dimension.jaffle_store_name"]


def test_plan_top_n_without_time_cue_ranks_whole_dimension(runtime_factory) -> None:
    """ "top stores by revenue" ranks stores overall. The defaulted month
    grain would rank store-months; only an explicit time cue keeps a bucket."""
    runtime = runtime_factory("jaffle_shop")
    try:
        ranked = plan_payload(runtime, intent="top stores by revenue")
        bucketed = plan_payload(runtime, intent="top stores by revenue by month")
    finally:
        runtime.close()
    ranked_query = ranked["best"]["query_ir"]
    assert ranked["status"] == "ok"
    assert "time" not in ranked_query
    assert ranked_query["limit"] == 5
    assert ranked_query["order_by"][0]["direction"] == "DESC"
    # An explicit grain cue keeps the time bucket.
    assert bucketed["best"]["query_ir"]["time"]["grain"] == "month"
