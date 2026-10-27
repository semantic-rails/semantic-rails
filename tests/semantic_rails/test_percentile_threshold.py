"""Phase 2 — inline percentile-as-threshold on metric_predicate.

Reviewer's exact failing pattern: a two-round-trip "compute P90 →
filter on P90" pipeline. The fix widens ``metric_predicate.value``
to accept ``{kind: "percentile", p: <float>}`` and emits a threshold
CTE in the lowered SQL so a single query both computes the threshold
and applies it.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

LIFETIME_SPEND = {
    "kind": "measure",
    "measure": "measure.jaffle.lifetime_spend_usd",
    "aggregation": "sum",
}
P90 = {"kind": "percentile", "p": 0.9}


def _revenue_of_customers_at_least(value: Any) -> dict[str, Any]:
    """Revenue from customers whose lifetime spend is at least ``value``."""
    predicate = {
        "kind": "metric_predicate",
        "entity": "entity.jaffle_customer",
        "over": "all_time",
        "input": LIFETIME_SPEND,
        "op": ">=",
        "value": value,
    }
    expression = {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.revenue_usd",
        "aggregation": "sum",
        "predicates": [predicate],
    }
    return {"select": [{"expression": expression, "as": "rev"}]}


def _value(result: dict[str, Any]) -> float:
    rows = result.get("rows") or []
    assert rows, f"empty result: {result!r}"
    return float(list(rows[0].values())[0] or 0.0)


def test_validate_and_compile_emit_threshold_cte_and_cross_join(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    query = _revenue_of_customers_at_least(P90)
    try:
        assert runtime.validate(query)["ok"]
        result = runtime.compile(query)
    finally:
        runtime.close()
    assert result["ok"], result.get("errors")
    sql = result["rendered_sql"].lower()
    # Threshold CTE landed alongside the predicate source CTE.
    assert "threshold" in sql
    # Percentile aggregator emitted — DuckDB renders as
    # ``quantile_cont``, Snowflake renders as ``percentile_cont
    # within group``. Either signals the threshold materialised.
    assert "quantile_cont" in sql or "percentile_cont" in sql
    # CROSS JOIN to the threshold CTE so the predicate reads from it.
    assert "cross join" in sql


def test_inline_percentile_matches_two_step_pipeline_within_tolerance(runtime_factory):
    """Sanity check: the inline form returns the same revenue total as
    the explicit two-step "compute P90 → filter ≥ P90" pipeline."""
    runtime = runtime_factory("jaffle_shop")
    distribution = {
        "kind": "distribution",
        "function": "percentile",
        "p": 0.9,
        "over": {
            "kind": "entity_value",
            "entity": "entity.jaffle_customer",
            "input": LIFETIME_SPEND,
        },
    }
    try:
        # Step 1 (two-step): compute P90 of lifetime_spend across customers.
        p90_rows = runtime.query({"select": [{"expression": distribution, "as": "p90"}]})["rows"]
        if not p90_rows:
            pytest.skip("Distribution P90 returned no rows on this seed")
        p90_value = float(list(p90_rows[0].values())[0])
        # Step 2 (two-step): revenue from customers whose lifetime_spend ≥ P90.
        two_step = _value(runtime.query(_revenue_of_customers_at_least(p90_value)))
        # Step 1+2 inline: same query in one shot.
        inline = _value(runtime.query(_revenue_of_customers_at_least(P90)))
    finally:
        runtime.close()
    # FP tolerance — the inline form computes the threshold in-engine
    # and the two-step form passes it as a literal, so they may
    # differ by sub-cent quantization on the float path.
    assert math.isclose(two_step, inline, rel_tol=1e-6, abs_tol=1e-3), (
        f"inline form value {inline} should match two-step {two_step}"
    )


@pytest.mark.parametrize(
    "threshold",
    [
        {"kind": "percentile", "p": 1.5},
        # Other expression kinds aren't supported as inline thresholds yet: the parser
        # surfaces a clear envelope rather than passing the dict through to a SqlLiteral wrap.
        {"kind": "measure", "measure": "measure.jaffle.revenue_usd"},
    ],
    ids=["p-outside-range", "unsupported-kind"],
)
def test_invalid_inline_threshold_returns_structured_error(runtime_factory, threshold):
    runtime = runtime_factory("jaffle_shop")
    try:
        errors = runtime.validate(_revenue_of_customers_at_least(threshold)).get("errors") or []
    finally:
        runtime.close()
    assert errors and errors[0]["code"] == "INVALID_EXPRESSION_AST"
    if threshold["kind"] == "percentile":
        assert "[0, 1]" in errors[0]["message"]
    else:
        assert "percentile" in errors[0].get("details", {})["supported_inline_kinds"]
