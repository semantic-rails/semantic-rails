"""Fail-closed checks for intent clauses that validate cannot prove."""

from __future__ import annotations

import pytest

from semantic_rails.planner import plan_payload


def _gap_kinds(payload: dict) -> set[str]:
    why = payload.get("why") or {}
    details = why.get("details") or {}
    return {str(row.get("kind")) for row in details.get("gaps") or []}


@pytest.mark.parametrize(
    ("intent", "expected_gap"),
    [
        (
            "Top 3 customers by revenue within each store",
            "partitioned_ranking_unrealized",
        ),
        (
            "Revenue by store for customers with at least 4 orders in the last 90 days "
            "compared with prior year",
            "prior_period_comparison_unrealized",
        ),
        (
            "Revenue and order count by store last quarter",
            "multiple_subjects_unrealized",
        ),
        (
            "Food or drink orders by store excluding Brooklyn",
            "negation_reversed",
        ),
    ],
)
def test_validating_but_unfaithful_complex_plans_fail_closed(
    runtime_factory,
    intent: str,
    expected_gap: str,
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()

    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert expected_gap in _gap_kinds(payload)
    assert payload["best"]["validation_ok"] is True
    assert "ready_for" not in payload["next"]
    assert payload["why"]["recovery_hints"]


@pytest.mark.parametrize(
    ("intent", "pattern"),
    [
        ("revenue vs prior year by store", "inline_period_shift"),
        ("revenue vs order count by store last quarter", "inline_comparison"),
        ("orders by store and month", "metric_by_dimension_rollup"),
        ("orders by store in Brooklyn", "metric_by_dimension_rollup"),
        (
            "Give me the sum of active menu snapshot at the store dimension for stores "
            "that have done more than one order and more than one session grouped by month",
            "qualified_metric_rollup",
        ),
        (
            "Give me the 28D adoption funnel from signup to Send for stores that have an "
            "order rate of over 90% grouped by month",
            "filtered_adoption_funnel",
        ),
        (
            "monthly orders for customers with at least 10 lifetime orders",
            "qualified_metric_rollup",
        ),
        (
            "food revenue share vs drink revenue share by store",
            "inline_comparison",
        ),
    ],
)
def test_faithfulness_gate_preserves_realized_and_supported_shapes(
    runtime_factory,
    intent: str,
    pattern: str,
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()

    assert payload["status"] == "ok", payload.get("why")
    assert payload["best"]["pattern"] == pattern
    assert payload["next"]["ready_for"] == ["compile", "execute"]


def test_positive_value_filter_is_not_mistaken_for_negation(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="orders by store in Brooklyn")
    finally:
        runtime.close()

    assert payload["status"] == "ok"
    assert payload["best"]["query_ir"]["where"] == [
        {"field": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}
    ]
