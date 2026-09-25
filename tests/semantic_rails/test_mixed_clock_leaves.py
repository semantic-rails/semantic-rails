"""A measure with several clocks, none of them the query's, is refused, not guessed.

In a query whose clock only some measures have, each other measure is timed by its
own clock. A measure with several clocks was timed by the first one it listed, with
only a ``REWRITE_APPLIED`` warning. A ratio of order-line revenue (order and delivery
clocks) over orders, queried on the order's delivery clock, divided order-date revenue
by delivered orders.
"""

from __future__ import annotations

import dataclasses

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry

_ORDERS = "measure.jaffle.order_count"
_ORDER_TIME = "temporal_role.jaffle_order_time"
_FIRST_ORDER = "temporal_role.jaffle_customer_first_order_at"


def _orders_per_session(overrides: dict[str, str]) -> dict:
    ratio = {
        "kind": "ratio",
        "numerator": {"kind": "aggregate", "measure": _ORDERS},
        "denominator": {"kind": "aggregate", "measure": "measure.jaffle.session_starts"},
    }
    return {
        "version": 2,
        "select": [{"expression": ratio, "as": "orders_per_session"}],
        "time": {
            "temporal_role": "temporal_role.jaffle_session_started_at",
            "grain": "month",
            "start": "2016-09-01",
            "end": "2016-10-01",
        },
        "temporal_role_overrides": overrides,
    }


def _with_order_clocks(config, clocks: list[str]):
    measures = [
        dataclasses.replace(row, compatible_temporal_roles=clocks) if row.id == _ORDERS else row
        for row in config.measures
    ]
    return dataclasses.replace(config, measures=measures)


def _answer(runtime, config, overrides: dict[str, str]) -> float:
    sql = compile_query(config, Registry(config), _orders_per_session(overrides))["sql"]
    [row] = runtime._get_adapter().query(sql)
    return row["orders_per_session"]


def test_a_measure_with_several_clocks_none_the_querys_is_refused(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    config = _with_order_clocks(config, [_ORDER_TIME, _FIRST_ORDER])

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), _orders_per_session({}))

    assert exc.value.code == "INCOMPATIBLE_TEMPORAL_ROLE"
    assert exc.value.details["measure"] == _ORDERS
    assert exc.value.details["compatible"] == [_ORDER_TIME, _FIRST_ORDER]
    assert "temporal_role_overrides" in str(exc.value)


@pytest.mark.parametrize(
    ("clocks", "overrides", "expected"),
    [
        # A named clock answers. The two differ, so the silent pick changed the answer.
        ([_ORDER_TIME, _FIRST_ORDER], {_ORDERS: _ORDER_TIME}, 136.7),
        ([_ORDER_TIME, _FIRST_ORDER], {_ORDERS: _FIRST_ORDER}, 1916.8),
        # A measure with one clock is still aligned to it, as before.
        ([_ORDER_TIME], {}, 136.7),
    ],
    ids=["named-order-clock", "named-first-order-clock", "one-clock"],
)
def test_a_named_or_single_clock_answers(runtime_factory, clocks, overrides, expected):
    runtime = runtime_factory("jaffle_shop")
    try:
        answer = _answer(runtime, _with_order_clocks(runtime.config, clocks), overrides)
    finally:
        runtime.close()

    assert answer == pytest.approx(expected)
