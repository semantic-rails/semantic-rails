"""A nested expression compiles in time proportional to its size: doubling its depth at most
doubles how often any engine function runs. Counted, never timed: a step that re-reads the
subtree under each node (checking its strings again, a recursive generator passing every
leaf up through each level above it) runs about four times as often at twice the depth."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import semantic_rails

REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
ENGINE = str(Path(semantic_rails.__file__).parent)
DEPTH = 40
# Linear growth doubles a count; the re-reads this guards against grew it 3.4 to 3.9 times.
MOST_GROWTH = 2.5


def _above(threshold: int, left: Any = ORDERS) -> dict[str, Any]:
    return {
        "kind": "comparison",
        "op": ">",
        "left": left,
        "right": {"kind": "literal", "value": threshold},
    }


def _arithmetic(depth: int) -> Any:
    node: Any = REVENUE
    for level in range(depth):
        node = {
            "kind": "arithmetic",
            "op": "add" if level % 2 else "subtract",
            "left": node,
            "right": ORDERS,
        }
    return node


def _case(depth: int) -> Any:
    node: Any = REVENUE
    for level in range(depth):
        node = {"kind": "case", "whens": [{"when": _above(level), "then": REVENUE}], "else": node}
    return node


def _prior_period(depth: int) -> Any:
    """A prior period of an arithmetic of a prior period, and so on."""
    node: Any = REVENUE
    for level in range(depth):
        if level % 2:
            node = {"kind": "prior_period", "input": node, "offset": {"unit": "month", "value": 1}}
        else:
            node = {"kind": "arithmetic", "op": "subtract", "left": node, "right": ORDERS}
    return node


def _boolean(depth: int) -> Any:
    node = _above(0)
    for level in range(depth):
        node = {"kind": "boolean", "op": "and", "args": [_above(level, REVENUE), node]}
    return node


def _row_filter(depth: int) -> Any:
    """Orders counted under a condition nested ``depth`` levels deep."""
    column = {"kind": "column", "entity": "entity.jaffle_order", "column": "customer_order_number"}
    node = _above(0, column)
    for level in range(depth):
        node = {"kind": "boolean", "op": "and", "args": [_above(level, column), node]}
    return {"kind": "aggregate_if", "aggregation": "count", "condition": node}


SHAPES = {
    "arithmetic": _arithmetic,
    "case": _case,
    "prior_period": _prior_period,
    "boolean": _boolean,
    "row_filter": _row_filter,
}


def _engine_calls(runtime: Any, call: dict[str, Any]) -> Counter[tuple[str, int, str]]:
    """How often each engine function starts or resumes while ``call`` validates."""
    counts: Counter[tuple[str, int, str]] = Counter()

    def profile(frame: Any, event: str, arg: Any) -> None:
        code = frame.f_code
        if event in {"call", "c_call"} and code.co_filename.startswith(ENGINE):
            name = code.co_name if event == "call" else f"{code.co_name} -> {arg.__qualname__}"
            counts[(code.co_filename, code.co_firstlineno, name)] += 1

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        answer = runtime.validate(call, now="2024-06-15T00:00:00Z")
    finally:
        sys.setprofile(previous)
    assert answer["status"] == "ok", answer.get("errors")
    return counts


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_twice_as_deep_runs_no_engine_function_more_than_twice_as_often(runtime_factory, shape):
    runtime = runtime_factory("jaffle_shop")
    try:
        counts = []
        for depth in (DEPTH, 2 * DEPTH):
            call: dict[str, Any] = {
                "select": [{"expression": SHAPES[shape](depth), "as": "value"}],
                "group_by": [STORE],
            }
            if shape == "prior_period":
                call["time"] = {"grain": "month"}
            counts.append(_engine_calls(runtime, call))
    finally:
        runtime.close()
    shallow, deep = counts
    grew = {
        f"{Path(key[0]).name}:{key[1]} {key[2]}": round(deep[key] / shallow[key], 2)
        for key in deep
        if shallow[key] >= DEPTH and deep[key] > MOST_GROWTH * shallow[key]
    }
    assert grew == {}
