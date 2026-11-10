"""A call nested deeper than the engine compiles is refused at its field on every surface, a
call at that depth compiles on every dialect with the host's share of the stack left over, and
a boolean of a thousand arguments compiles."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import Any, TypeVar

import pytest

from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import boolean_chain_depth
from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.query_call import MAX_CALL_DEPTH, call_depth
from semantic_rails.renderer import render_expr
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlIdentifier, boolean_chain

REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
NUMBER = {"kind": "column", "entity": "entity.jaffle_order", "column": "customer_order_number"}
NOW = "2024-06-15T00:00:00Z"
# Frames a program calling the engine may hold below it (the engine has the rest of Python's
# recursion limit) at the deepest call the engine compiles.
HOST_FRAMES = 200
T = TypeVar("T")


def _compared(threshold: int, left: Any = ORDERS) -> dict[str, Any]:
    return {
        "kind": "comparison",
        "op": ">",
        "left": left,
        "right": {"kind": "literal", "value": threshold},
    }


def _arithmetic(levels: int) -> Any:
    node: Any = REVENUE
    for level in range(levels):
        node = {
            "kind": "arithmetic",
            "op": "add" if level % 2 else "subtract",
            "left": node,
            "right": ORDERS,
        }
    return node


def _prior_period(levels: int) -> Any:
    """A prior period of an arithmetic of a prior period, and so on: the most stack a level."""
    node: Any = REVENUE
    for level in range(levels):
        if level % 2:
            node = {"kind": "prior_period", "input": node, "offset": {"unit": "month", "value": 1}}
        else:
            node = {"kind": "arithmetic", "op": "subtract", "left": node, "right": ORDERS}
    return node


def _case(levels: int) -> Any:
    node: Any = REVENUE
    for level in range(levels):
        node = {
            "kind": "case",
            "whens": [{"when": _compared(level), "then": REVENUE}],
            "else": node,
        }
    return node


def _nullif(levels: int) -> Any:
    node: Any = REVENUE
    for level in range(levels):
        node = {"kind": "nullif", "value": node, "null_value": {"kind": "literal", "value": level}}
    return node


def _row_filter(levels: int) -> Any:
    node = _compared(0, NUMBER)
    for level in range(levels):
        node = {"kind": "boolean", "op": "and", "args": [_compared(level, NUMBER), node]}
    return {"kind": "aggregate_if", "aggregation": "count", "condition": node}


SHAPES: dict[str, Callable[[int], Any]] = {
    "arithmetic": _arithmetic,
    "prior_period": _prior_period,
    "case": _case,
    "nullif": _nullif,
    "row_filter": _row_filter,
}


def _call(expression: Any) -> dict[str, Any]:
    call: dict[str, Any] = {
        "select": [{"expression": expression, "as": "value"}],
        "group_by": [STORE],
    }
    if "prior_period" in repr(expression):
        call["time"] = {"grain": "month"}
    return call


def _deepest(shape: str) -> dict[str, Any]:
    """The deepest call of ``shape`` the engine compiles."""
    levels = 1
    while call_depth(_call(SHAPES[shape](levels + 1)))[0] <= MAX_CALL_DEPTH:
        levels += 1
    call = _call(SHAPES[shape](levels))
    assert call_depth(call)[0] >= MAX_CALL_DEPTH - 1
    return call


def _with_host_frames(run: Callable[[], T]) -> T:
    """``run()`` with :data:`HOST_FRAMES` frames in use below it, as a calling program's own."""
    frame, in_use = sys._getframe(), 0
    while frame is not None:
        frame, in_use = frame.f_back, in_use + 1
    leave = sys.getrecursionlimit() - (1000 - HOST_FRAMES)

    def pad(count: int) -> T:
        return pad(count - 1) if count > 0 else run()

    return pad(max(0, leave - in_use))


@pytest.fixture
def runtime(runtime_factory) -> Any:
    rt = runtime_factory("jaffle_shop")
    yield rt
    rt.close()


def _refused(runtime: Runtime, surface: str, call: dict[str, Any]) -> dict[str, Any]:
    if surface == "runtime":
        answer = runtime.validate(call, now=NOW)
        assert answer["status"] == "error"
        return answer["errors"][0]
    if surface == "mcp":
        answer = SemanticLayerMCPAdapter(runtime).call_tool(
            "execute", {"query": call, "mode": "run"}
        )
        assert answer["ok"] is False
        return answer["errors"][0]
    service = SemanticHTTPService(runtime)
    with pytest.raises(SemanticLayerError) as caught:
        service.handle("POST", normalize_route("/api/v1/query"), {"query": call})
    answer, status = service.exception_payload(caught.value, stage="http")
    assert status == 400
    return answer["errors"][0]


@pytest.mark.parametrize("surface", ["runtime", "http", "mcp"])
def test_a_call_deeper_than_the_engine_compiles_is_refused_at_its_field(runtime, surface):
    error = _refused(runtime, surface, _call(_arithmetic(200)))
    assert error["code"] == "INVALID_QUERY"
    assert error["message"] == (
        "select[0].expression: is 203 levels deep in the call; "
        "the engine compiles a call at most 100 levels deep"
    )
    assert {key: error["details"][key] for key in ("path", "depth", "max_depth")} == {
        "path": "select[0].expression",
        "depth": 203,
        "max_depth": MAX_CALL_DEPTH,
    }
    assert error["recovery_hints"][0]["code"] == "FIX_QUERY_CALL"


def test_a_joined_part_too_deep_is_refused_in_that_part(runtime):
    joined = {
        "parts": [{"select": [ORDERS], "group_by": [STORE]}, _call(_arithmetic(150))],
        "join": {"on": [[STORE, STORE]]},
    }
    error = runtime.validate(joined, now=NOW)["errors"][0]
    assert error["code"] == "INVALID_QUERY"
    assert error["message"] == (
        "parts[1]: select[0].expression: is 153 levels deep in the call; "
        "the engine compiles a call at most 100 levels deep"
    )
    assert error["details"]["path"] == "parts[1].select[0].expression"
    assert error["details"]["part"] == 1


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_deepest_call_answers_with_the_host_frames_left(runtime, shape):
    call = _deepest(shape)
    assert _with_host_frames(lambda: runtime.validate(call, now=NOW))["status"] == "ok"
    # DuckDB binds a nested NULLIF in time exponential in its depth, and no warehouse nests one
    # window function in another, as a prior period of a prior period would.
    if shape not in {"nullif", "prior_period"}:
        assert _with_host_frames(lambda: runtime.query(call, now=NOW))["status"] == "ok"


@pytest.mark.parametrize("warehouse", sorted(supported_warehouses()))
def test_every_dialect_renders_the_deepest_call_with_the_host_frames_left(warehouse):
    path = resolve_repo_path("configs/semantic_rails/jaffle_shop")
    config = load_package_config(path)
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    runtime = Runtime.from_config(config, source_path=path)
    try:
        # The two that take the most stack a level; every shape peaks alike on every dialect.
        for shape in ("nullif", "prior_period"):
            try:
                runtime.compile(_call(SHAPES[shape](2)), now=NOW)
            except SemanticLayerError:
                continue  # the dialect has no form for this shape at any depth
            call = _deepest(shape)
            assert _with_host_frames(partial(runtime.compile, call, now=NOW))["ok"], shape
    finally:
        runtime.close()


def test_a_boolean_of_a_thousand_arguments_answers_as_its_in(runtime):
    values = list(range(1, 1001))
    either = {
        "kind": "boolean",
        "op": "or",
        "args": [
            {
                "kind": "comparison",
                "op": "=",
                "left": NUMBER,
                "right": {"kind": "literal", "value": value},
            }
            for value in values
        ],
    }
    listed = {"kind": "in", "expr": NUMBER, "values": values}
    answers = [
        runtime.query(
            _call({"kind": "aggregate_if", "aggregation": "count", "condition": condition}), now=NOW
        )
        for condition in (either, listed)
    ]
    assert answers[0]["status"] == answers[1]["status"] == "ok"
    assert answers[0]["rows"] == answers[1]["rows"]
    # Its arguments count as deep as they are joined: 21 levels, not 999.
    pair = {**either, "args": either["args"][:2]}
    assert (
        call_depth(_call(either))[0] - call_depth(_call(pair))[0] == boolean_chain_depth(1000) - 1
    )
    assert boolean_chain_depth(1000) == 21


def test_a_long_boolean_joins_its_halves_and_a_short_one_reads_as_written():
    names = [SqlIdentifier([name]) for name in "abcdefghijklmnopq"]
    assert render_expr(boolean_chain("AND", names[:16])) == " AND ".join("abcdefghijklmnop")
    assert (
        render_expr(boolean_chain("OR", names))
        == "a OR b OR c OR d OR e OR f OR g OR h OR i OR (j OR k OR l OR m OR n OR o OR p OR q)"
    )
