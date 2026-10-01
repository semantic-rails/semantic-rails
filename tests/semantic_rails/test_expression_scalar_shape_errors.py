"""A scalar where an expression payload needs an object is a structured error.

``{kind: "rolling", input, window: 28}`` (int window) used to crash with
``INTERNAL_ERROR: TypeError: 'int' object is not iterable`` because the
parser called ``dict(28)``, so callers got a bare Python exception instead of
the structured-error contract. The parser now raises ``INVALID_EXPRESSION_AST``
(or the expression's own required-field code) with a ``USE_OBJECT_SHAPE``
recovery hint naming the right shape.
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
