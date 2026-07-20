"""Regression tests for ``time.grain`` validation.

Before this validator, ``validate(payload)`` would happily accept a
typo like ``grain: "fortnight"`` because the compiler never tested the
grain string against the temporal role's ``supported_grains``. The
mistake only surfaced at execute time as a raw DuckDB error
("extract specifier 'fortnight' not recognized") with no structured
recovery hint and no list of valid grains.

These tests pin the contract: validate must reject unsupported grains
structurally, list the supported ones, and emit a ``replace_grain``
recovery hint when a near-miss is available.
"""

from __future__ import annotations

from semantic_rails.mcp import SemanticLayerMCPAdapter


def _select_revenue() -> list[dict[str, object]]:
    return [
        {
            "expression": {"measure": "measure.jaffle.revenue_usd"},
            "as": "revenue",
        }
    ]


def test_unknown_grain_rejected_with_closest_matches(runtime_factory):
    """``fortnight`` is not a valid grain — validate must reject it
    with a structured INVALID_QUERY envelope listing supported grains
    and at least one closest match."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": _select_revenue(),
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "fortnight",
                },
            }
        )
        assert report["ok"] is False
        assert report["errors"], "expected at least one error"
        err = report["errors"][0]
        assert err["code"] == "INVALID_QUERY"
        details = err.get("details") or {}
        assert details.get("path") == "time.grain"
        assert details.get("received") == "fortnight"
        assert details.get("temporal_role") == "temporal_role.jaffle_order_time"
        # jaffle_order_time supports day/week/month/quarter/year
        supported = list(details.get("supported_grains") or [])
        assert set(supported) >= {"day", "week", "month", "quarter", "year"}
        closest = list(details.get("closest_matches") or [])
        assert closest, "closest_matches must be populated for fortnight"
        # The replace_grain recovery hint should be available.
        hints = list(report.get("recovery_hints") or [])
        assert hints, "recovery_hints must be populated for typo grain"
        replace_hints = [h for h in hints if h.get("kind") == "replace_grain"]
        assert replace_hints, f"expected a replace_grain hint, got {hints!r}"
        patch = replace_hints[0].get("patch") or {}
        assert "replace" in patch and "time.grain" in patch["replace"]
    finally:
        runtime.close()


def test_grain_unsupported_by_specific_role_rejected(runtime_factory):
    """``temporal_role.jaffle_monthly_metric_month`` only supports
    month/quarter/year per its YAML — week must be rejected, even
    though it's a globally valid grain elsewhere."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "measure": ("measure.jaffle_monthly_metrics.prior_period_revenue_usd")
                        },
                        "as": "prior_revenue",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_monthly_metric_month",
                    "grain": "week",
                },
            }
        )
        assert report["ok"] is False
        err = report["errors"][0]
        assert err["code"] == "INVALID_QUERY"
        details = err.get("details") or {}
        assert details.get("path") == "time.grain"
        assert details.get("received") == "week"
        supported = list(details.get("supported_grains") or [])
        assert supported == ["month", "quarter", "year"]
        assert "week" not in supported
    finally:
        runtime.close()


def test_empty_grain_passes(runtime_factory):
    """Empty/missing grain is allowed — separate ungrained-time-projection
    warnings handle that case. The grain validator must not raise."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": _select_revenue(),
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    # No grain — should not raise INVALID_QUERY from grain check.
                },
            }
        )
        # The query may still emit warnings about ungrained-time projection,
        # but it must not error out on the grain check.
        grain_errors = [
            e
            for e in (report.get("errors") or [])
            if e.get("code") == "INVALID_QUERY"
            and (e.get("details") or {}).get("path") == "time.grain"
        ]
        assert not grain_errors, f"empty grain must not raise, got {grain_errors!r}"
    finally:
        runtime.close()


def test_uppercase_grain_normalized_and_passes(runtime_factory):
    """The validator normalizes grain to lowercase before checking
    against ``supported_grains``. ``"Month"`` is treated as ``"month"``
    and passes — consistent with the codebase's de-facto invariant
    of lowercasing grain at every downstream use site."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": _select_revenue(),
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "Month",
                },
            }
        )
        grain_errors = [
            e
            for e in (report.get("errors") or [])
            if e.get("code") == "INVALID_QUERY"
            and (e.get("details") or {}).get("path") == "time.grain"
        ]
        assert not grain_errors, f"'Month' must normalize to 'month', got {grain_errors!r}"
        # And the overall query should compile.
        assert report["ok"] is True
    finally:
        runtime.close()


def test_mcp_adapter_surfaces_grain_error_with_recovery_hints(runtime_factory):
    """End-to-end probe through the MCP adapter: the response envelope
    must carry the INVALID_QUERY code on ``errors[0]``, surface it at
    the top-level ``error`` key, and aggregate ``recovery_hints`` at
    the top level so agents reading the conventional envelope shape
    can recover."""
    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        out = adapter.call_tool(
            "validate",
            {
                "query": {
                    "select": _select_revenue(),
                    "time": {
                        "temporal_role": "temporal_role.jaffle_order_time",
                        "grain": "fortnight",
                    },
                }
            },
        )
        assert out.get("ok") is False
        errors = list(out.get("errors") or [])
        assert errors, "adapter must surface errors list"
        first = errors[0]
        assert first.get("code") == "INVALID_QUERY"
        # Top-level `error` mirrors errors[0] for conventional MCP clients.
        top_error = out.get("error") or {}
        assert top_error.get("code") == "INVALID_QUERY"
        # closest_matches and supported_grains are present in details.
        details = first.get("details") or {}
        assert details.get("received") == "fortnight"
        assert details.get("closest_matches"), "closest_matches must be set"
        # recovery_hints aggregated at the envelope top level.
        top_hints = list(out.get("recovery_hints") or [])
        replace_hints = [h for h in top_hints if h.get("kind") == "replace_grain"]
        assert replace_hints, (
            f"expected replace_grain in top-level recovery_hints, got {top_hints!r}"
        )
    finally:
        runtime.close()


def test_valid_grain_still_compiles(runtime_factory):
    """Sanity check: existing valid queries with ``grain: "month"``
    must still compile after the validator is added — we must not
    accidentally strip or reject good grains."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": _select_revenue(),
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                },
            }
        )
        assert report["ok"] is True, f"valid grain query must compile: {report.get('errors')}"
    finally:
        runtime.close()
