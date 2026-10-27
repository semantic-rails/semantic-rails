"""A window object accepts only the keys the compiler reads.

Before this was refused, `rolling.window: {..., exclude_current: true}` or
`conversion.window: {..., include_end: true}` validated and returned the default window's
number, as if the request had been understood.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened


def _measure(measure_id: str) -> dict[str, Any]:
    """An operand as the parser reads it; a call may also send the bare id."""
    return {"kind": "measure", "measure": measure_id}


def _rolling(window: dict[str, Any], ref=_measure) -> dict[str, Any]:
    return {"kind": "rolling", "input": ref("measure.jaffle.revenue_usd"), "window": window}


def _conversion(window: dict[str, Any], ref=_measure) -> dict[str, Any]:
    return {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": window,
        "matching_mode": "first_converted_after_base",
        "base": ref("measure.jaffle.session_starts"),
        "converted": ref("measure.jaffle.order_count"),
    }


def _metric_predicate(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "metric_predicate",
        "entity": "entity.jaffle_customer",
        "input": {"kind": "aggregate", "measure": "order_count"},
        "op": ">",
        "value": 1,
        "scope_mode": "entity_only",
        "window": window,
    }


def _scoped_aggregate(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.revenue_usd",
        "aggregation": "sum",
        "anchor": {"temporal_role": "temporal_role.jaffle_customer_first_order_at"},
        "window": window,
    }


# The conversion window keeps its own error family, so its recovery shape still applies.
AST, CONVERSION = "INVALID_EXPRESSION_AST", "CONVERSION_WINDOW_REQUIRED"


@pytest.mark.parametrize(
    ("kind", "build", "unknown", "valid", "code"),
    [
        ("rolling", _rolling, {"exclude_current": True}, ["unit", "value"], AST),
        ("rolling", _rolling, {"grain": "month"}, ["unit", "value"], AST),
        ("conversion", _conversion, {"include_end": True}, ["unit", "value"], CONVERSION),
        ("conversion", _conversion, {"bounds": "(]"}, ["unit", "value"], CONVERSION),
        ("metric_predicate", _metric_predicate, {"inclusive": False}, ["unit", "value"], AST),
        (
            "scoped_aggregate",
            _scoped_aggregate,
            {"offset": 1},
            ["direction", "unit", "value"],
            AST,
        ),
    ],
)
def test_an_unknown_window_key_is_refused(kind, build, unknown, valid, code) -> None:
    expression = build({"unit": "day", "value": 7, **unknown})
    with pytest.raises(SemanticLayerError) as raised:
        parse_semantic_expression(expression, context="query")
    error = raised.value
    assert error.code == code
    assert error.details["path"] == "expression.window"
    assert error.details["expression_kind"] == kind
    assert error.details["unknown_window_keys"] == sorted(unknown)
    assert error.details["valid_window_keys"] == valid
    assert f"{kind} 'window' has unsupported key(s) {sorted(unknown)}" in str(error)


def test_the_conversion_refusal_states_the_window_it_applies() -> None:
    with pytest.raises(SemanticLayerError) as raised:
        parse_semantic_expression(
            _conversion({"unit": "day", "value": 7, "include_end": True}), context="query"
        )
    assert "at or after the base event and before the base event plus the window" in str(
        raised.value
    )


@pytest.mark.parametrize(
    "expression",
    [
        _rolling({"unit": "month", "value": 3}),
        _conversion({"unit": "day", "value": 7}),
        _metric_predicate({"unit": "day", "value": 30}),
        _scoped_aggregate({"unit": "day", "value": 90, "direction": "forward"}),
    ],
)
def test_the_accepted_window_keys_still_parse(expression) -> None:
    parse_semantic_expression(expression, context="query")


@pytest.fixture(scope="module")
def runtime() -> Iterator[Runtime]:
    rt = Runtime("jaffle_shop")
    try:
        yield opened(rt)
    finally:
        rt.close()


MONTHLY = {"time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}}
SESSION_MONTHLY = {
    "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "month"}
}


@pytest.mark.parametrize(
    ("build", "time", "unknown"),
    [
        pytest.param(_rolling, MONTHLY, {"exclude_current": True}, id="rolling"),
        pytest.param(_conversion, SESSION_MONTHLY, {"include_end": True}, id="conversion"),
    ],
)
def test_a_query_with_an_unknown_window_key_is_refused_not_answered(
    runtime, build, time, unknown
) -> None:
    window = {"unit": "month" if build is _rolling else "day", "value": 3}

    def call(window: dict[str, Any]) -> dict[str, Any]:
        expression = build(window, ref=lambda measure_id: measure_id)
        return {"select": [{"expression": expression, "as": "value"}], **time}

    accepted = call(window)
    assert runtime.validate(accepted)["ok"], runtime.validate(accepted).get("errors")
    refused = call({**window, **unknown})
    result = runtime.validate(refused)
    assert not result["ok"]
    (error,) = result["errors"]
    assert error["code"] == (CONVERSION if build is _conversion else AST)
    assert error["details"]["unknown_window_keys"] == sorted(unknown)
