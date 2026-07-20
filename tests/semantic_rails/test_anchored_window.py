"""Phase 2 (round-three) — per-row event-anchored windows on
``scoped_aggregate``. The IR contract lands in this round; the SQL
lowering is staged for the next round (see the
``feature_pending_sql_lowering`` recovery hint).
"""

from __future__ import annotations


def test_validate_accepts_canonical_anchor_window_shape(runtime_factory):
    """Parser accepts the canonical shape — anchor + window — and
    surfaces the structured ``INVALID_ANCHOR_ROLE`` envelope (with
    ``feature_status: ir_contract_only``) at compile time."""
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
                            "anchor": {
                                "temporal_role": "temporal_role.jaffle_customer_first_order_at",
                            },
                            "window": {
                                "unit": "day",
                                "value": 90,
                                "direction": "forward",
                            },
                        },
                        "as": "rev_90d_post_acquisition",
                    }
                ],
            }
        )
        # IR validates against the parser (no INVALID_EXPRESSION_AST)
        # but the compile-stub blocks with INVALID_ANCHOR_ROLE so the
        # caller gets a clear signal that execution isn't supported
        # yet. The feature_status marker proves we hit the stub path.
        errors = result.get("errors") or []
        assert errors, f"expected INVALID_ANCHOR_ROLE stub envelope, got {result!r}"
        first = errors[0]
        assert first["code"] == "INVALID_ANCHOR_ROLE"
        assert first["details"].get("feature_status") == "ir_contract_only"
        hints = first.get("recovery_hints") or []
        pending = [h for h in hints if h.get("kind") == "feature_pending_sql_lowering"]
        assert pending, f"expected feature_pending_sql_lowering hint, got {hints!r}"
    finally:
        runtime.close()


def test_validate_rejects_half_specified_anchor_window(runtime_factory):
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
                            # ``window`` present without ``anchor`` — bad shape.
                            "window": {"unit": "day", "value": 90},
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors and errors[0]["code"] == "INVALID_EXPRESSION_AST"
        assert "anchor" in str(errors[0]["message"]).lower()
    finally:
        runtime.close()


def test_validate_rejects_unknown_anchor_role(runtime_factory):
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
                            "anchor": {
                                "temporal_role": "temporal_role.this_does_not_exist",
                            },
                            "window": {"unit": "day", "value": 90, "direction": "forward"},
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors and errors[0]["code"] == "INVALID_ANCHOR_ROLE"
        details = errors[0].get("details") or {}
        # Available roles surface so the agent doesn't have to call inspect.
        assert details.get("available_temporal_roles")
        # The recovery hint lists them via ``use_valid_anchor_role``.
        hints = errors[0].get("recovery_hints") or []
        valid_role_hints = [h for h in hints if h.get("kind") == "use_valid_anchor_role"]
        assert valid_role_hints
    finally:
        runtime.close()


def test_validate_rejects_invalid_window_unit(runtime_factory):
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
                            "anchor": {
                                "temporal_role": "temporal_role.jaffle_customer_first_order_at",
                            },
                            "window": {"unit": "fortnight", "value": 6},
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors and errors[0]["code"] == "INVALID_EXPRESSION_AST"
        assert "window.unit" in errors[0]["message"]
    finally:
        runtime.close()


def test_validate_rejects_invalid_window_direction(runtime_factory):
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
                            "anchor": {
                                "temporal_role": "temporal_role.jaffle_customer_first_order_at",
                            },
                            "window": {"unit": "day", "value": 90, "direction": "sideways"},
                        },
                        "as": "rev",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors and errors[0]["code"] == "INVALID_EXPRESSION_AST"
        assert "direction" in errors[0]["message"]
    finally:
        runtime.close()


def test_scoped_aggregate_without_anchor_window_still_works(runtime_factory):
    """Regression: existing scoped_aggregate (no anchor/window) must
    behave exactly as before — Phase 2 must not break the round-two
    percentile-threshold path."""
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
                        "as": "rev_top_decile",
                    }
                ],
            }
        )
        assert result["ok"], result.get("errors")
    finally:
        runtime.close()
