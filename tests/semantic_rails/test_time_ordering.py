"""Time axes have a deterministic default order on every SQL dialect."""

from dataclasses import replace

import pytest

from semantic_rails.compiler import _compile_query_sql_ast, compile_query, lower_to_sql
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.config import load_package_config
from semantic_rails.dialects import dialect_for_warehouse, supported_warehouses
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.conftest import copy_package_config


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    package = copy_package_config(tmp_path_factory.mktemp("ordering"), "jaffle_shop")
    return load_package_config(str(package))


ROLE = "temporal_role.jaffle_order_time"
TIME = "period"
GROUPS = ["dimension.jaffle_store_name", "dimension.jaffle_store_id"]


def _refuses_fill(warehouse, fill):
    # Every fill uses the implicit calendar; an authored one takes no part.
    return fill and not dialect_for_warehouse(warehouse).has_implicit_calendar


def _query(*, fill=False, groups=(), **extra):
    return {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "group_by": list(groups),
        "time": {"temporal_role": ROLE, "grain": "month", "fill": fill},
        **extra,
    }


# One warehouse per dialect family: DuckLake and MotherDuck render as DuckDB does.
DIALECTS = [w for w in supported_warehouses() if w not in {"ducklake", "motherduck"}]


# Every dialect family, each with one of the fill and group shapes; ClickHouse refuses a fill.
@pytest.mark.parametrize(
    ("warehouse", "fill", "groups"),
    [
        ("athena", False, []),
        ("bigquery", True, GROUPS),
        ("clickhouse", False, GROUPS),
        ("clickhouse", True, GROUPS[::-1]),
        ("databricks", False, GROUPS[::-1]),
        ("duckdb", True, GROUPS[::-1]),
        ("duckdb", False, GROUPS),
        ("postgres", True, GROUPS),
        ("snowflake", False, GROUPS[::-1]),
    ],
)
def test_default_order_is_time_then_authored_groups(config, warehouse, fill, groups):
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    if _refuses_fill(warehouse, fill):
        with pytest.raises(SemanticLayerError, match="no implicit calendar"):
            compile_query(config, None, _query(fill=fill, groups=groups, limit=3))
        return
    compiled = compile_query(config, None, _query(fill=fill, groups=groups, limit=3))
    # The default order already holds every group_by column and the period: a limit adds no
    # tie-breaker, and never a measure.
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
        [f"  {quote}{TIME}{quote} ASC"]
        + [f"  {quote}{aliases.get(alias, alias)}{quote} ASC" for alias in groups]
    )


@pytest.mark.parametrize(
    ("warehouse", "fill", "field"),
    [
        (warehouse, index % 2 == 1, ["time", "orders", GROUPS[0]][index % 3])
        for index, warehouse in enumerate(DIALECTS)
    ],
)
def test_explicit_order_wins_without_extra_tiebreakers(config, fill, field, warehouse):
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    query = _query(fill=fill, groups=GROUPS, order_by=[{"field": field, "direction": "DESC"}])
    compiled = compile_query(config, None, query)
    assert len(compiled["sql_ast"].order_by) == 1
    assert compiled["sql_ast"].order_by[0].direction == "DESC"


@pytest.mark.parametrize("order_by", [None, []])
def test_empty_explicit_order_uses_the_default(config, order_by):
    compiled = compile_query(config, None, _query(order_by=order_by))
    assert compiled["sql"].endswith(f'\nORDER BY\n  "{TIME}" ASC')


def test_raw_time_axis_is_ordered(config):
    query = _query(groups=GROUPS)
    del query["time"]["grain"]
    compiled = compile_query(config, None, query)
    assert compiled["sql_ast"].order_by[0].expression.parts == [ROLE]


@pytest.mark.parametrize("shape", ["combined", "distinct", "metric-filter", "anchored"])
def test_default_order_covers_alternate_lowering_paths(config, shape):
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
        # The opening balance: a closing-day balance's share by entity sets is refused.
        aggregate = {
            "kind": "scoped_aggregate",
            "measure": "measure.jaffle.inventory_on_hand_sop",
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
    assert [o.expression.parts for o in compiled["sql_ast"].order_by] == [
        [TIME],
        *[[group] for group in GROUPS],
    ]


# Exercise every lowering shape on DuckDB and each dialect's outer ordering on
# one distribution. Nested ORDER BY suppression is independent of SQL rendering.
# Nested PostgreSQL distributions also exercise long generated CTE identifiers.
@pytest.mark.parametrize(
    "warehouse,shape",
    [
        ("duckdb", shape)
        for shape in (
            "distribution",
            "branches",
            "nested-distribution",
            "predicate",
            "metric-filter",
        )
    ]
    + [(warehouse, "distribution") for warehouse in DIALECTS if warehouse != "duckdb"]
    + [("postgres", "nested-distribution")],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_only_the_request_result_is_ordered(config, warehouse, explicit, shape):
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    query = _query(groups=GROUPS[:1])
    measure = {"measure": "measure.jaffle.order_count"}

    def distribution(input_, entity="entity.jaffle_order"):
        return {
            "kind": "distribution",
            "function": "avg",
            "over": {"kind": "entity_value", "entity": entity, "input": input_},
        }

    expression = distribution(measure)
    if shape == "nested-distribution":
        expression = distribution(expression, "entity.jaffle_customer")
    elif shape in {"predicate", "metric-filter"}:
        expression = {
            "kind": "scoped_aggregate",
            "measure": "measure.jaffle.revenue_usd",
            "aggregation": "sum",
            "predicates": [
                {
                    "entity": "entity.jaffle_customer",
                    "input": measure,
                    "op": ">",
                    "value": 0,
                    "time_alignment": "same_query_period",
                }
            ],
        }
    query["select"] = [{"expression": expression, "as": "value"}]
    if shape == "metric-filter":
        query["select"] = [{"expression": measure, "as": "orders"}]
        query["metric_filters"] = [
            {
                "expression": {"kind": "metric_predicate", **expression["predicates"][0]},
                "op": "=",
                "value": True,
            }
        ]
    if shape == "branches":
        query["select"].append({"expression": measure, "as": "orders"})
    if explicit:
        query["order_by"] = [{"field": "time", "direction": "DESC"}]
    compiled = compile_query(config, None, query)
    assert compiled["sql_ast"].order_by
    assert compiled["sql"].count("\nORDER BY\n") == 1
    assert [o.direction for o in compiled["sql_ast"].order_by] == (
        ["DESC"] if explicit else ["ASC", "ASC"]
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_nested_query_preserves_only_explicit_order(config, explicit):
    query = _query(limit=3)
    if explicit:
        query["order_by"] = [{"field": "time", "direction": "DESC"}]
    select = _compile_query_sql_ast(config, query)
    assert select.limit == 3
    # The period is the only group key and is ordered already: no tie-breaker follows.
    assert [o.direction for o in select.order_by] == (["DESC"] if explicit else [])
    if explicit:
        assert select.order_by[-1].nulls_last


@pytest.mark.parametrize("missing", [TIME, GROUPS[0]])
def test_missing_default_order_key_is_refused(config, monkeypatch, missing):
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
def test_without_a_time_axis_no_default_order_is_added(config, window):
    query = _query(groups=GROUPS)
    if window:
        query["time"] = {"temporal_role": ROLE, "start": "2017-04-01", "end": "2017-05-01"}
    else:
        del query["time"]
    compiled = compile_query(config, None, query)
    assert compiled["sql_ast"].order_by == []
