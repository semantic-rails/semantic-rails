"""Boolean ``not`` in expression parsing and in every lowering path.

``not`` was folded as a binary chain, so ``{kind: boolean, op: not, args: [<cmp>]}`` returned
the un-negated argument, and parsing accepted any ``op``, failing later at render time. The
post-aggregation, measure-expression and relation-pipeline lowerings each had the same fold.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails.compiler_parts.bind import _config_expr_to_sql
from semantic_rails.compiler_parts.post_aggregation import _compile_post_expr
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    BooleanExpr,
    ComparisonExpr,
    LiteralExpr,
    parse_semantic_expression,
)
from semantic_rails.relation_pipelines import _semantic_expr_to_sql
from semantic_rails.renderer import render_expr
from tests.semantic_rails.conftest import copy_package_config

ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop")
    return load_package_config(str(path))


def _orders_over(threshold: int, orders: Any = None) -> dict:
    """``orders > threshold``; the engine's own form unless a call's reference is given."""
    return {
        "kind": "comparison",
        "op": ">",
        "left": orders or {"kind": "measure", "measure": ORDERS},
        "right": {"kind": "literal", "value": threshold},
    }


def _boolean(op: str, count: int) -> dict:
    return {"kind": "boolean", "op": op, "args": [_orders_over(n) for n in range(count)]}


def test_parse_normalizes_the_case_of_each_boolean_op():
    for raw_op, count in [("not", 1), ("NOT", 1), ("Not", 1), ("AND", 2), ("Or", 2)]:
        parsed = parse_semantic_expression(_boolean(raw_op, count), context="query")
        assert isinstance(parsed, BooleanExpr)
        assert (parsed.op, len(parsed.args)) == (raw_op.lower(), count)


def test_parse_rejects_an_unknown_op_a_binary_not_and_no_args():
    for op, count, details, hint in [
        (
            "xor",
            2,
            {"allowed": ["and", "or", "not"], "received_value": "xor"},
            "USE_SUPPORTED_BOOLEAN_OP",
        ),
        ("not", 2, {"received_arg_count": 2}, "WRAP_NOT_ARGS"),
        ("and", 0, {}, ""),
    ]:
        with pytest.raises(SemanticLayerError) as exc:
            parse_semantic_expression(_boolean(op, count), context="query")
        assert exc.value.code == "INVALID_EXPRESSION_AST"
        assert details.items() <= exc.value.details.items(), op
        if hint:
            assert exc.value.details["recovery_hints"][0]["code"] == hint


ONE_OVER_FIVE = ComparisonExpr(op=">", left=LiteralExpr(1), right=LiteralExpr(5))
THREE_UNDER_FOUR = ComparisonExpr(op="<", left=LiteralExpr(3), right=LiteralExpr(4))
LOWERINGS = {
    "post_aggregation": lambda expr, config: _compile_post_expr(expr, config),
    "measure_expression": lambda expr, config: _config_expr_to_sql(
        expr, config.measures[0], config
    ),
    "relation_pipeline": lambda expr, config: _semantic_expr_to_sql(expr),
}


@pytest.mark.parametrize("lowering", sorted(LOWERINGS))
def test_each_lowering_negates_not_and_refuses_two_arguments(config, lowering):
    # Negation is ``FALSE = (arg)``: NOT's three-valued truth table, parenthesized for chains.
    lower = LOWERINGS[lowering]
    negated = lower(BooleanExpr(op="not", args=[ONE_OVER_FIVE]), config)
    assert render_expr(negated) == "FALSE = (1 > 5)"
    with pytest.raises(SemanticLayerError) as exc:
        lower(BooleanExpr(op="not", args=[ONE_OVER_FIVE, ONE_OVER_FIVE]), config)
    assert exc.value.code == "INVALID_EXPRESSION_AST"


def test_post_aggregation_lowering_folds_and_and_refuses_other_shapes(config):
    two = [ONE_OVER_FIVE, THREE_UNDER_FOUR]
    assert _compile_post_expr(BooleanExpr(op="and", args=two), config).op == "AND"
    for op, args, details in [
        ("not", two, {"received_arg_count": 2}),
        ("xor", two, {"allowed": ["and", "or", "not"]}),
        ("and", [], {}),
    ]:
        with pytest.raises(SemanticLayerError) as exc:
            _compile_post_expr(BooleanExpr(op=op, args=args), config)
        assert exc.value.code == "INVALID_EXPRESSION_AST"
        assert details.items() <= exc.value.details.items(), op


def test_metric_filter_not_executes_as_complement(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    base = {"select": [{"expression": ORDERS, "as": "orders"}], "group_by": [STORE]}
    try:
        counts = {row[STORE]: row["orders"] for row in runtime.query(base)["rows"]}
        assert len(counts) > 1
        # Split the stores: ``> threshold`` keeps the strictly-larger ones,
        # ``not (> threshold)`` must keep exactly the rest.
        threshold = min(counts.values())
        kept = {}
        for negate in (True, False):
            expression = _orders_over(threshold, ORDERS)
            if negate:
                expression = {"kind": "boolean", "op": "not", "args": [expression]}
            query = {
                **base,
                "select": [*base["select"], {"expression": expression, "as": "kept"}],
                "having": [{"field": "kept", "op": "is", "value": True}],
            }
            kept[negate] = {row[STORE] for row in runtime.query(query)["rows"]}
    finally:
        runtime.close()

    assert kept[True] == {store for store, count in counts.items() if not count > threshold}
    assert kept[False] == {store for store, count in counts.items() if count > threshold}
