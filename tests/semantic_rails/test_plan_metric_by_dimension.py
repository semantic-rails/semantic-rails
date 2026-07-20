"""A4: ``metric_by_dimension_rollup`` pattern.

The pattern names the common "measure / metric (by dimension) (top N)"
intent shape. It must:

1. Fire from ``plan`` on representative simple intents.
2. Decline intents that should fire a more specific earlier pattern
   (thresholds, period shifts, X-vs-Y).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from semantic_rails.expressions import AggregateExpr, ColumnRefExpr
from semantic_rails.planner import compose
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MetricConfig,
    PackageConfig,
    PackageMeta,
)

_FIRES_FROM_PROPOSE = [
    ("top stores by revenue", "measure.jaffle.revenue_usd"),
    ("monthly revenue by store", "measure.jaffle.revenue_usd"),
    ("orders by store", "measure.jaffle.order_count"),
]


@pytest.mark.parametrize("intent,expected_target", _FIRES_FROM_PROPOSE)
def test_metric_by_dimension_fires_from_plan(
    runtime_factory, intent: str, expected_target: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, intent)
    finally:
        runtime.close()
    assert result.pattern == "metric_by_dimension_rollup", (
        f"plan did not fire metric_by_dimension_rollup for {intent!r}; "
        f"got pattern={result.pattern!r}"
    )
    target = result.draft.interpreted_intent.get("target")
    assert target == expected_target, (
        f"resolved wrong target for {intent!r}: got {target!r}, expected {expected_target!r}"
    )


def test_metric_by_dimension_without_time_cue_does_not_invent_a_bucket(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, "orders by store")
    finally:
        runtime.close()

    query = result.draft.query
    assert query["select"] == [
        {
            "as": "order_count",
            "expression": {
                "measure": "measure.jaffle.order_count",
                "aggregation": "count_distinct",
            },
        }
    ]
    assert query["group_by"] == ["dimension.jaffle_store_name"]
    assert "time" not in query


@pytest.mark.parametrize(
    ("intent", "expected_target"),
    [
        ("total orders", "measure.jaffle.order_count"),
        ("total revenue", "measure.jaffle.revenue_usd"),
        ("average order value", "metric.sales.aov_usd"),
    ],
)
def test_bare_aggregate_without_time_cue_stays_all_time(
    runtime_factory, intent: str, expected_target: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, intent)
    finally:
        runtime.close()

    assert result.draft is not None
    query = result.draft.query
    assert "time" not in query
    expression = query["select"][0]["expression"]
    assert (expression.get("measure") or expression.get("metric")) == expected_target


def test_metric_by_dimension_preserves_an_explicit_time_series_intent(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, "historical revenue by customer segment")
    finally:
        runtime.close()

    assert result.draft.query["time"] == {
        "temporal_role": "temporal_role.jaffle_order_time",
        "grain": "month",
    }


@pytest.mark.parametrize(
    "intent,expected_winner",
    [
        ("top 3 stores by revenue with at least 4 customers", "qualified_metric_rollup"),
        ("revenue YoY", "inline_period_shift"),
        ("food vs drink revenue", "inline_comparison"),
        ("signup to send adoption", "filtered_adoption_funnel"),
    ],
)
def test_specific_patterns_win_over_catch_all(
    runtime_factory, intent: str, expected_winner: str
) -> None:
    """The catch-all returns ``score=0.5`` so any more
    specific pattern (score=1.0) wins via score-based selection.
    """

    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, intent)
    finally:
        runtime.close()
    assert result.pattern == expected_winner, (
        f"{intent!r}: expected specific pattern {expected_winner!r} to win on score, "
        f"got pattern={result.pattern!r}"
    )


def _claims_config() -> PackageConfig:
    return PackageConfig(
        version=3,
        package=PackageMeta(package_id="claims", name="Claims", description="", warehouse="duckdb"),
        entities=[
            EntityConfig(id="entity.claim", table="claims", primary_key="claim_id"),
        ],
        dimensions=[
            DimensionConfig(
                id="dimension.claim_primary_diagnosis_code",
                entity="entity.claim",
                column="primary_diagnosis_code",
                data_type="string",
                label="Primary diagnosis code",
            ),
            DimensionConfig(
                id="dimension.claim_admission_date",
                entity="entity.claim",
                column="admission_date",
                data_type="date",
                label="Claim admission date",
            ),
        ],
        temporal_roles=[],
        relationships=[],
        value_domains=[],
        measures=[
            MeasureConfig(
                id="measure.claim_paid_amount",
                entity="entity.claim",
                subject_entity="entity.claim",
                aggregation_entity="entity.claim",
                row_grain=["claim_id"],
                expr=ColumnRefExpr("claim_paid_amount"),
                default_aggregation="sum",
                allowed_aggregations=["sum"],
                label="Claim paid amount",
            ),
            MeasureConfig(
                id="measure.primary_payer_paid_amount",
                entity="entity.claim",
                subject_entity="entity.claim",
                aggregation_entity="entity.claim",
                row_grain=["claim_id"],
                expr=ColumnRefExpr("primary_payer_paid_amount"),
                default_aggregation="sum",
                allowed_aggregations=["sum"],
                label="Primary payer paid amount",
            ),
        ],
        metric_recipes=[
            MetricConfig(
                id="metric.claim_paid_amount",
                kind="aggregate",
                expression=AggregateExpr("measure.claim_paid_amount", "sum"),
                label="Claim paid amount",
            )
        ],
    )


def test_top_n_group_by_phrase_uses_after_by_as_measure_target() -> None:
    """CMS-style phrasing must not let group words pick the target measure."""

    runtime = SimpleNamespace(config=_claims_config())
    result = compose(runtime, "Top five primary diagnosis codes by claim paid amount")

    assert result.pattern == "metric_by_dimension_rollup"
    assert result.draft is not None
    query = result.draft.query
    assert query["select"][0]["expression"]["measure"] == "measure.claim_paid_amount"
    assert query["group_by"] == ["dimension.claim_primary_diagnosis_code"]
    assert query["order_by"] == [{"field": "claim_paid_amount", "direction": "DESC"}]
    assert query["limit"] == 5
    assert result.intent_ir.subjects[0].id == "measure.claim_paid_amount"
    assert "five" not in result.intent_ir.unresolved
