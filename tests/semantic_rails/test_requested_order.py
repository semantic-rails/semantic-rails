"""Every requested order_by term sorts NULLs last, in both directions, on every backend."""

from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError

ORDERS = {"measure": "measure.jaffle.order_count"}
INVENTORY = {
    "kind": "scoped_aggregate",
    "measure": "measure.jaffle.inventory_on_hand_eop",
    "aggregation": "sum",
}
# Each lowering path that builds the final ORDER BY, by the source its requested term reads.
SHAPES = {
    "inlined": ("", {"select": [{"expression": ORDERS, "as": "ranked"}]}),
    "metric-filter": (
        "projected.",
        {
            "select": [{"expression": ORDERS, "as": "ranked"}],
            "metric_filters": [{"expression": ORDERS, "op": ">", "value": 0}],
        },
    ),
    "anchored-ratio": (
        "anchor.",
        {
            "select": [
                {
                    "as": "ranked",
                    "expression": {
                        "kind": "ratio",
                        "numerator": {
                            **INVENTORY,
                            "predicates": [
                                {
                                    "entity": "entity.jaffle_store",
                                    "measure": "measure.jaffle.order_count",
                                    "op": ">",
                                    "value": 0,
                                    "time_alignment": "same_query_period",
                                }
                            ],
                        },
                        "denominator": INVENTORY,
                    },
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
        },
    ),
    "distribution": (
        "agent_projected.",
        {
            "select": [
                {
                    "as": "ranked",
                    "expression": {
                        "kind": "distribution",
                        "function": "avg",
                        "over": {
                            "kind": "entity_value",
                            "entity": "entity.jaffle_order",
                            "input": ORDERS,
                        },
                    },
                }
            ]
        },
    ),
}


def _query(shape: str, direction: str) -> dict:
    return {
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        "order_by": [{"field": "ranked", "direction": direction}],
        **SHAPES[shape][1],
    }


@pytest.mark.parametrize("warehouse", supported_warehouses())
@pytest.mark.parametrize("direction", ["ASC", "DESC"])
@pytest.mark.parametrize("shape", list(SHAPES))
def test_requested_term_renders_nulls_last(package_config_factory, warehouse, direction, shape):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    compiled = compile_query(config, None, _query(shape, direction))
    if shape == "anchored-ratio":
        assert any(
            row["kind"] == "anchored_entity_set" for row in compiled["physical_plan"].optimizations
        )
    assert [(term.direction, term.nulls_last) for term in compiled["sql_ast"].order_by] == [
        (direction, True)
    ]
    order_sql = compiled["sql"].rsplit("\nORDER BY\n", 1)[1].split("\n", 1)[0]
    assert order_sql == f"  {SHAPES[shape][0]}ranked {direction} NULLS LAST"


@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_requested_term_without_nulls_last_is_refused(
    package_config_factory, monkeypatch, shape
):
    config, _ = package_config_factory("jaffle_shop")
    requested_order = sql_lowering._requested_order
    monkeypatch.setattr(
        sql_lowering,
        "_requested_order",
        lambda *args: [replace(term, nulls_last=False) for term in requested_order(*args)],
    )
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, None, _query(shape, "DESC"))
    assert caught.value.code == "INVALID_ORDER_BY"
