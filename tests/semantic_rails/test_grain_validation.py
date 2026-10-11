"""Regression tests for ``time.grain`` validation.

Before this validator, ``validate(payload)`` would happily accept a
typo like ``grain: "fortnight"`` because the compiler never tested the
grain string against the temporal role's ``supported_grains``. The
mistake only surfaced at execute time as a raw DuckDB error
("extract specifier 'fortnight' not recognized") with no structured
recovery hint and no list of valid grains.

These tests pin the contract: validate must reject unsupported grains
structurally and name the supported ones.
"""

from __future__ import annotations

from semantic_rails.mcp import SemanticLayerMCPAdapter


def _select_revenue() -> list[dict[str, object]]:
    return [{"expression": "measure.jaffle.revenue_usd", "as": "revenue"}]


def _grain_refusal(error: dict) -> None:
    """A grain outside the call's words is refused at ``time.grain`` with the one hint."""
    assert error["code"] == "INVALID_QUERY"
    assert (error.get("details") or {}).get("path") == "time.grain"
    (hint,) = error["recovery_hints"]
    assert hint["code"] == "FIX_QUERY_CALL"
    assert "day, week, month, quarter or year" in hint["message"]


def test_unknown_grain_rejected_with_the_grains(runtime_factory):
    """``fortnight`` is not a valid grain — validate must reject it
    with a structured INVALID_QUERY envelope naming the grains."""
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
        _grain_refusal(report["errors"][0])
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
                        "expression": "measure.jaffle_monthly_metrics.prior_period_revenue_usd",
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
    """A window without a grain is one total. The grain validator must not raise."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": _select_revenue(),
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    # No grain — should not raise INVALID_QUERY from grain check.
                    "start": "2016-09-01",
                    "end": "2016-10-01",
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


def test_uppercase_grain_is_refused(runtime_factory):
    """Grains are lowercase words, like every word of the call: ``"Month"`` is refused
    with the grains rather than read as ``"month"``."""
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
        assert report["ok"] is False
        _grain_refusal(report["errors"][0])
    finally:
        runtime.close()


def test_mcp_adapter_surfaces_grain_error_with_recovery_hints(runtime_factory):
    """End-to-end probe through the MCP adapter: the response envelope
    must carry the INVALID_QUERY code on ``errors[0]`` with its hint and
    surface it at the top-level ``error`` key."""
    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        out = adapter.call_tool(
            "execute",
            {
                "mode": "validate",
                "query": {
                    "select": _select_revenue(),
                    "time": {
                        "temporal_role": "temporal_role.jaffle_order_time",
                        "grain": "fortnight",
                    },
                },
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
        _grain_refusal(first)
    finally:
        runtime.close()
