"""Round-six PR B contracts — error path fixes.

Two findings from the post-round-five blind-agent benchmark:

  B3 — ``{kind: "rolling", input, window: 28}`` (int window) used to
       crash with ``INTERNAL_ERROR: TypeError: 'int' object is not
       iterable`` because the parser called ``dict(28)``. The structured-
       error contract was broken — agents got a bare Python exception.
       Now the parser raises ``INVALID_EXPRESSION_AST`` with a
       ``USE_OBJECT_SHAPE`` recovery hint naming the right shape.

  B5 — ``WINDOWED_TIME_FILTER_UNSUPPORTED`` used to ship
       ``widen_time_window`` first and ``drop_time_start`` second.
       Blind agents grabbing the first hint applied a widen that
       sometimes still failed (the prior_period LAG needs at least
       one full bucket before the widened start). Round six reorders
       so ``drop_time_start`` (always idempotent) is first.
"""

from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression


def test_rolling_window_int_returns_structured_error():
    """Passing ``window`` as an int (the shape the broken capabilities
    example used to teach) must raise INVALID_EXPRESSION_AST with a
    USE_OBJECT_SHAPE recovery hint — NOT TypeError."""
    with pytest.raises(SemanticLayerError) as excinfo:
        parse_semantic_expression(
            {
                "kind": "rolling",
                "input": {"measure": "measure.jaffle.revenue_usd"},
                "window": 28,
            },
            context="query",
        )
    err = excinfo.value
    assert err.code == "INVALID_EXPRESSION_AST", (
        f"expected INVALID_EXPRESSION_AST, got {err.code!r}"
    )
    hints = err.details.get("recovery_hints") or []
    codes = {h.get("code") for h in hints}
    assert "USE_OBJECT_SHAPE" in codes, (
        f"expected USE_OBJECT_SHAPE recovery hint; got hints={hints!r}"
    )
    assert err.details.get("received_type") == "int"
    assert err.details.get("received_value") == 28


def test_prior_period_offset_int_returns_structured_error():
    """The IR-shape prior_period path also called dict() on the offset
    payload — passing offset as a bare int (without ``measure`` to
    trigger the shorthand path) crashed. Now it raises structurally."""
    with pytest.raises(SemanticLayerError) as excinfo:
        parse_semantic_expression(
            {
                "kind": "prior_period",
                "input": {"measure": "measure.jaffle.revenue_usd"},
                "offset": 1,
            },
            context="query",
        )
    err = excinfo.value
    assert err.code == "INVALID_EXPRESSION_AST"
    hints = err.details.get("recovery_hints") or []
    codes = {h.get("code") for h in hints}
    assert "USE_OBJECT_SHAPE" in codes, (
        f"expected USE_OBJECT_SHAPE recovery hint; got hints={hints!r}"
    )


def test_conversion_window_int_returns_structured_error():
    """Conversion expressions had the same dict(int) crash on window."""
    with pytest.raises(SemanticLayerError) as excinfo:
        parse_semantic_expression(
            {
                "kind": "conversion",
                "base": {"measure": "measure.jaffle.session_count"},
                "converted": {"measure": "measure.jaffle.order_count"},
                "entity": "entity.jaffle_order",
                "window": 7,
                "matching_mode": "first_converted_after_base",
            },
            context="query",
        )
    err = excinfo.value
    assert err.code == "CONVERSION_WINDOW_REQUIRED"
    hints = err.details.get("recovery_hints") or []
    codes = {h.get("code") for h in hints}
    assert "USE_OBJECT_SHAPE" in codes


def test_windowed_time_filter_hints_drop_first_then_widen(runtime_factory):
    """Round-six reorder: drop_time_start (always safe) must come first;
    widen_time_window (sometimes off-by-one for prior_period at month
    grain) must come second. Blind agents typically apply the first
    hint — make sure the first hint is the safe path."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.prior_week_revenue_direct"},
                        "as": "v",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "day",
                    "start": "2017-03-15",
                },
            }
        )
        errors = result.get("errors") or []
        first = next(e for e in errors if e.get("code") == "WINDOWED_TIME_FILTER_UNSUPPORTED")
        hints = list(first.get("recovery_hints") or [])
        assert hints, "expected recovery hints on WINDOWED_TIME_FILTER_UNSUPPORTED"
        assert hints[0].get("kind") == "drop_time_start", (
            f"expected first hint to be drop_time_start (idempotent); got {hints[0].get('kind')!r}"
        )
        # widen_time_window may or may not appear depending on whether
        # suggested_start can be computed — but if present, it's after drop.
        widen_idx = next(
            (i for i, h in enumerate(hints) if h.get("kind") == "widen_time_window"),
            None,
        )
        if widen_idx is not None:
            assert widen_idx > 0, f"widen_time_window should not be first; got hints={hints!r}"
    finally:
        runtime.close()
