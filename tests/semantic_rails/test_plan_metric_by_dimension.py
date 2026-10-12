"""A4: ``metric_by_dimension_rollup`` pattern.

The pattern names the common "measure / metric (by dimension) (top N)"
intent shape. It must:

1. Fire from ``plan`` on representative simple intents.
2. Decline intents that should fire a more specific earlier pattern
   (thresholds, period shifts, X-vs-Y).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.expressions import AggregateExpr, ColumnRefExpr
from semantic_rails.planner import compose
from semantic_rails.planner.call import Call
from semantic_rails.planner.generators import _choose_group_dimensions
from semantic_rails.runtime import Runtime
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MetricConfig,
    PackageConfig,
    PackageMeta,
)
from tests.semantic_rails.planner_fixtures import jaffle_runtime

ROLLUP = "metric_by_dimension_rollup"
REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
MONTHLY = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}


runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


@pytest.mark.parametrize(
    ("intent", "pattern", "subject", "time"),
    [
        ("top stores by revenue", ROLLUP, REVENUE, None),
        ("monthly revenue by store", ROLLUP, REVENUE, MONTHLY),
        # No time cue invents no bucket.
        ("orders by store", ROLLUP, ORDERS, None),
        # A bare aggregate with no time cue stays all time.
        ("total orders", None, ORDERS, None),
        ("total revenue", None, REVENUE, None),
        ("average order value", None, "metric.sales.aov_usd", None),
        # An explicit time-series intent keeps its series.
        ("historical revenue by customer segment", None, None, MONTHLY),
        # A more specific pattern (score 1.0) wins over the catch-all (score 0.5).
        ("top 3 stores by revenue with at least 4 customers", "qualified_metric_rollup", None, ...),
        ("revenue YoY", "inline_period_shift", None, ...),
        ("food vs drink revenue", "inline_comparison", None, ...),
        ("revenue versus order count by store", "inline_comparison", None, ...),
        ("signup to send adoption", "filtered_adoption_funnel", None, ...),
    ],
)
def test_compose_picks_the_pattern_subject_and_time(
    runtime, intent: str, pattern: str | None, subject: str | None, time: Any
) -> None:
    result = compose(runtime, intent)
    assert result.draft is not None
    query = result.draft.query.to_call()
    if pattern:
        assert result.pattern == pattern, (intent, result.pattern)
    if subject:
        assert result.draft.query.select[0].ref == subject
    if pattern == ROLLUP:
        assert result.draft.interpreted_intent.get("target") == subject
    if time is not ...:
        assert query.get("time") == time
    if intent == "orders by store":
        assert query["group_by"] == ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]
    if pattern == "inline_comparison":
        interpreted = result.draft.interpreted_intent
        assert interpreted["left"]["id"] and interpreted["right"]["id"]
        assert interpreted["left"]["id"] != interpreted["right"]["id"]
        assert len(query["select"]) == 2


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
                row_grain=["claim_id"],
                expr=ColumnRefExpr("claim_paid_amount"),
                default_aggregation="sum",
                allowed_aggregations=["sum"],
                label="Claim paid amount",
            ),
            MeasureConfig(
                id="measure.primary_payer_paid_amount",
                entity="entity.claim",
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

    runtime = Runtime.from_config(_claims_config(), source_path="")
    try:
        result = compose(runtime, "Top five primary diagnosis codes by claim paid amount")
    finally:
        runtime.close()

    assert result.pattern == "metric_by_dimension_rollup"
    assert result.draft is not None
    query = result.draft.query.to_call()
    # The metric of the same name is the governed reading of "claim paid amount".
    assert query["select"] == ["metric.claim_paid_amount"]
    assert query["group_by"] == ["dimension.claim_primary_diagnosis_code"]
    assert query["order_by"] == [{"field": "claim_paid_amount", "direction": "desc"}]
    assert query["limit"] == 5
    assert result.intent_ir.subjects[0].id == "measure.claim_paid_amount"
    assert "five" not in result.intent_ir.unresolved


def test_fallback_discovery_keeps_region_instead_of_a_planner_synonym(runtime, monkeypatch) -> None:
    import semantic_rails.metadata as metadata

    geo = DimensionConfig(
        id="dimension.jaffle_item_geo",
        entity="entity.jaffle_item",
        column="geo",
        data_type="string",
        label="Geo",
    )
    monkeypatch.setattr(
        runtime, "_config", replace(runtime._config, dimensions=[*runtime._config.dimensions, geo])
    )
    discover = metadata.discover_payload
    discovered_terms = []

    def capture_discovery(*args, **kwargs):
        discovered_terms.append(kwargs["terms"])
        return discover(*args, **kwargs)

    monkeypatch.setattr(metadata, "discover_payload", capture_discovery)
    groups = _choose_group_dimensions(runtime, Call(), "item revenue by region")
    assert discovered_terms == ["region"]
    assert geo.id not in groups
