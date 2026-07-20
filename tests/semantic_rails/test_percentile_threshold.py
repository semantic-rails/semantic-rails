"""Phase 2 — inline percentile-as-threshold on metric_predicate.

Reviewer's exact failing pattern: a two-round-trip "compute P90 →
filter on P90" pipeline. The fix widens ``metric_predicate.value``
to accept ``{kind: "percentile", p: <float>}`` and emits a threshold
CTE in the lowered SQL so a single query both computes the threshold
and applies it.
"""

from __future__ import annotations

import math

import pytest


def test_validate_accepts_inline_percentile_threshold(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                            "predicates": [
                                {
                                    "kind": "metric_predicate",
                                    "entity": "entity.jaffle_customer",
                                    "input": {
                                        "measure": "measure.jaffle.lifetime_spend_usd",
                                        "aggregation": "sum",
                                    },
                                    "op": ">=",
                                    "value": {"kind": "percentile", "p": 0.9},
                                }
                            ],
                        },
                        "as": "revenue_top_decile",
                    }
                ],
            }
        )
        assert result["ok"], result.get("errors")
    finally:
        runtime.close()


def test_compile_emits_threshold_cte_and_cross_join(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.compile(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                            "predicates": [
                                {
                                    "kind": "metric_predicate",
                                    "entity": "entity.jaffle_customer",
                                    "input": {
                                        "measure": "measure.jaffle.lifetime_spend_usd",
                                        "aggregation": "sum",
                                    },
                                    "op": ">=",
                                    "value": {"kind": "percentile", "p": 0.9},
                                }
                            ],
                        },
                        "as": "revenue_top_decile",
                    }
                ],
            }
        )
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
    finally:
        runtime.close()


def test_inline_percentile_matches_two_step_pipeline_within_tolerance(runtime_factory):
    """Sanity check: the inline form returns the same revenue total as
    the explicit two-step "compute P90 → filter ≥ P90" pipeline."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # Step 1 (two-step): compute P90 of lifetime_spend across customers.
        p90_result = runtime.query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "distribution",
                            "function": "percentile",
                            "p": 0.9,
                            "over": {
                                "kind": "entity_value",
                                "entity": "entity.jaffle_customer",
                                "input": {
                                    "measure": "measure.jaffle.lifetime_spend_usd",
                                    "aggregation": "sum",
                                },
                            },
                        },
                        "as": "p90",
                    }
                ],
            }
        )
        p90_rows = p90_result.get("rows") or []
        if not p90_rows:
            pytest.skip("Distribution P90 returned no rows on this seed")
        p90_value = float(list(p90_rows[0].values())[0])
        # Step 2 (two-step): revenue from customers whose lifetime_spend ≥ P90.
        two_step = runtime.query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                            "predicates": [
                                {
                                    "kind": "metric_predicate",
                                    "entity": "entity.jaffle_customer",
                                    "input": {
                                        "measure": "measure.jaffle.lifetime_spend_usd",
                                        "aggregation": "sum",
                                    },
                                    "op": ">=",
                                    "value": p90_value,
                                }
                            ],
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        # Step 1+2 inline: same query in one shot.
        inline = runtime.query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                            "predicates": [
                                {
                                    "kind": "metric_predicate",
                                    "entity": "entity.jaffle_customer",
                                    "input": {
                                        "measure": "measure.jaffle.lifetime_spend_usd",
                                        "aggregation": "sum",
                                    },
                                    "op": ">=",
                                    "value": {"kind": "percentile", "p": 0.9},
                                }
                            ],
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        two_rows = two_step.get("rows") or []
        inline_rows = inline.get("rows") or []
        assert two_rows, f"two-step pipeline returned empty: {two_step!r}"
        assert inline_rows, f"inline form returned empty: {inline!r}"
        two_value = float(list(two_rows[0].values())[0] or 0.0)
        inline_value = float(list(inline_rows[0].values())[0] or 0.0)
        # FP tolerance — the inline form computes the threshold in-engine
        # and the two-step form passes it as a literal, so they may
        # differ by sub-cent quantization on the float path.
        assert math.isclose(two_value, inline_value, rel_tol=1e-6, abs_tol=1e-3), (
            f"inline form value {inline_value} should match two-step {two_value}"
        )
    finally:
        runtime.close()


def test_invalid_inline_threshold_p_outside_range_returns_structured_error(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                            "predicates": [
                                {
                                    "kind": "metric_predicate",
                                    "entity": "entity.jaffle_customer",
                                    "input": {
                                        "measure": "measure.jaffle.lifetime_spend_usd",
                                        "aggregation": "sum",
                                    },
                                    "op": ">=",
                                    "value": {"kind": "percentile", "p": 1.5},
                                }
                            ],
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors and errors[0]["code"] == "INVALID_EXPRESSION_AST"
        assert "[0, 1]" in errors[0]["message"]
    finally:
        runtime.close()


def test_unsupported_threshold_kind_returns_structured_error(runtime_factory):
    """Other expression kinds aren't supported as inline thresholds yet —
    the parser must surface a clear envelope rather than silently passing
    the dict through to a SqlLiteral wrap."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                            "predicates": [
                                {
                                    "kind": "metric_predicate",
                                    "entity": "entity.jaffle_customer",
                                    "input": {
                                        "measure": "measure.jaffle.lifetime_spend_usd",
                                        "aggregation": "sum",
                                    },
                                    "op": ">=",
                                    # Unsupported inline threshold kind.
                                    "value": {
                                        "kind": "measure",
                                        "measure": "measure.jaffle.revenue_usd",
                                    },
                                }
                            ],
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors and errors[0]["code"] == "INVALID_EXPRESSION_AST"
        details = errors[0].get("details", {})
        assert "supported_inline_kinds" in details
        assert "percentile" in details["supported_inline_kinds"]
    finally:
        runtime.close()
