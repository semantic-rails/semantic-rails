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

REVENUE = {"measure": "measure.jaffle.revenue_usd"}


@pytest.mark.parametrize(
    "raw,key,code",
    [
        ({"kind": "rolling", "input": REVENUE, "window": 28}, "window", "INVALID_EXPRESSION_AST"),
        # The IR shape (no ``measure``, so not the shorthand) reads offset as an object.
        (
            {"kind": "prior_period", "input": REVENUE, "offset": 1},
            "offset",
            "INVALID_EXPRESSION_AST",
        ),
        (
            {
                "kind": "conversion",
                "base": {"measure": "measure.jaffle.session_count"},
                "converted": {"measure": "measure.jaffle.order_count"},
                "entity": "entity.jaffle_order",
                "window": 7,
                "matching_mode": "first_converted_after_base",
            },
            "window",
            "CONVERSION_WINDOW_REQUIRED",
        ),
    ],
)
def test_a_scalar_where_an_object_belongs_is_a_structured_error(raw, key, code):
    with pytest.raises(SemanticLayerError) as excinfo:
        parse_semantic_expression(raw, context="query")
    details = excinfo.value.details
    assert excinfo.value.code == code
    assert details["path"] == f"expression.{key}"
    assert (details["received_type"], details["received_value"]) == ("int", raw[key])
    (hint,) = details["recovery_hints"]
    assert hint["code"] == "USE_OBJECT_SHAPE"
    assert set(hint["suggested_shape"]) == {"unit", "value"}
    # The suggested shape, filled in, parses.
    parse_semantic_expression({**raw, key: {"unit": "day", "value": raw[key]}}, context="query")
