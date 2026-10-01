"""Time axes have a deterministic default order on every SQL dialect."""

from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query, lower_to_sql
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError

ROLE = "temporal_role.jaffle_order_time"
TIME = f"{ROLE}__month"
GROUPS = ["dimension.jaffle_store_name", "dimension.jaffle_store_id"]


def _query(*, fill=False, groups=(), **extra):
    return {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "group_by": list(groups),
        "time": {"temporal_role": ROLE, "grain": "month", "fill": fill},
        **extra,
    }


@pytest.mark.parametrize("warehouse", supported_warehouses())
@pytest.mark.parametrize("fill", [False, True], ids=["sparse", "filled"])
@pytest.mark.parametrize(
    "groups", [[], GROUPS, GROUPS[::-1]], ids=["time-only", "groups", "reversed"]
)
def test_default_order_is_time_then_authored_groups(
    package_config_factory, warehouse, fill, groups
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    compiled = compile_query(config, None, _query(fill=fill, groups=groups, limit=3))
    expected = [TIME, *groups]
    assert [(o.expression.parts, o.direction) for o in compiled["sql_ast"].order_by] == [
        ([alias], "ASC") for alias in expected
    ]
    # Check the executable outer clause, including warehouses that rename output aliases.
    aliases = {
        semantic: physical for physical, semantic in compiled["prepared_query"].column_mapping
    }
    quote = "`" if warehouse in {"bigquery", "databricks"} else '"'
    order_sql = compiled["sql"].rsplit("\nORDER BY\n", 1)[1].split("\nLIMIT ", 1)[0]
    assert order_sql == ",\n".join(
        f"  {quote}{aliases.get(alias, alias)}{quote} ASC" for alias in expected
    )


@pytest.mark.parametrize("fill", [False, True])
@pytest.mark.parametrize("field", ["time", "orders", GROUPS[0]])
@pytest.mark.parametrize("warehouse", supported_warehouses())
def test_explicit_order_wins_without_extra_tiebreakers(
    package_config_factory, fill, field, warehouse
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    compiled = compile_query(
        config,
        None,
        _query(fill=fill, groups=GROUPS, order_by=[{"field": field, "direction": "DESC"}]),
    )
    assert len(compiled["sql_ast"].order_by) == 1
    assert compiled["sql_ast"].order_by[0].direction == "DESC"


@pytest.mark.parametrize("order_by", [None, []])
def test_empty_explicit_order_uses_the_default(package_config_factory, order_by):
    config, _ = package_config_factory("jaffle_shop")
    compiled = compile_query(config, None, _query(order_by=order_by))
    assert compiled["sql"].endswith(f'\nORDER BY\n  "{TIME}" ASC')


def test_raw_time_axis_is_ordered(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = _query(groups=GROUPS)
    del query["time"]["grain"]
    compiled = compile_query(config, None, query)
    assert compiled["sql_ast"].order_by[0].expression.parts == [ROLE]


@pytest.mark.parametrize("shape", ["combined", "distinct", "metric-filter", "anchored"])
def test_default_order_covers_alternate_lowering_paths(package_config_factory, shape):
    config, _ = package_config_factory("jaffle_shop")
    query = _query(groups=GROUPS)
    if shape == "combined":
        query["select"].append(
            {"expression": {"measure": "measure.jaffle.item_count"}, "as": "items"}
        )
    elif shape == "distinct":
        query["select"] = []
    elif shape == "metric-filter":
        query["metric_filters"] = [
            {"expression": {"measure": "measure.jaffle.order_count"}, "op": ">", "value": 0}
        ]
    else:
        aggregate = {
            "kind": "scoped_aggregate",
            "measure": "measure.jaffle.inventory_on_hand_eop",
            "aggregation": "sum",
        }
        query["select"] = [
            {
                "as": "share",
                "expression": {
                    "kind": "ratio",
                    "numerator": {
                        **aggregate,
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
                    "denominator": aggregate,
                },
            }
        ]
        query["time"]["temporal_role"] = "temporal_role.jaffle_inventory_day"
    compiled = compile_query(config, None, query)
    if shape == "anchored":
        assert any(
            row["kind"] == "anchored_entity_set" for row in compiled["physical_plan"].optimizations
        )
    expected_time = f"{query['time']['temporal_role']}__month"
    assert [o.expression.parts for o in compiled["sql_ast"].order_by] == [
        [expected_time],
        *[[group] for group in GROUPS],
    ]


@pytest.mark.parametrize("missing", [TIME, GROUPS[0]])
def test_missing_default_order_key_is_refused(package_config_factory, monkeypatch, missing):
    config, _ = package_config_factory("jaffle_shop")
    compiled = compile_query(config, None, _query(groups=GROUPS))
    select = compiled["sql_ast"]
    monkeypatch.setattr(
        sql_lowering,
        "_lower_query_to_sql",
        lambda *args: replace(select, select=[f for f in select.select if f.alias != missing]),
    )
    with pytest.raises(SemanticLayerError) as caught:
        lower_to_sql(compiled["logical_plan"], config)
    assert caught.value.code == "INVALID_ORDER_BY"


@pytest.mark.parametrize("window", [False, True], ids=["no-time", "window-total"])
def test_without_a_time_axis_no_default_order_is_added(package_config_factory, window):
    config, _ = package_config_factory("jaffle_shop")
    query = _query(groups=GROUPS)
    if window:
        query["time"] = {"temporal_role": ROLE, "start": "2017-04-01", "end": "2017-05-01"}
    else:
        del query["time"]
    compiled = compile_query(config, None, query)
    assert compiled["sql_ast"].order_by == []
