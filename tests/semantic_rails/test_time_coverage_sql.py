"""Coverage SQL portability and the boundaries where extra reads can change an answer."""

from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.compiler_parts.empty_groups import sql_nodes
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_select
from semantic_rails.sql_ast import SqlCall, SqlExists, SqlSelect

ROLE = "temporal_role.jaffle_order_time"
REVENUE = {"measure": "measure.jaffle.revenue_usd"}
ORDERS = {"measure": "measure.jaffle.order_count"}
NOW = {
    "duckdb": "TIMEZONE('America/New_York', NOW())",
    "postgres": "TIMEZONE('America/New_York', NOW())",
    "snowflake": "CAST(CONVERT_TIMEZONE('America/New_York', CURRENT_TIMESTAMP()) AS TIMESTAMP_NTZ)",
    "bigquery": "CURRENT_DATETIME('America/New_York')",
    "databricks": "CONVERT_TIMEZONE('America/New_York', CURRENT_TIMESTAMP())",
    "clickhouse": "NOW('America/New_York')",
    "athena": "CAST(AT_TIMEZONE(NOW(), 'America/New_York') AS TIMESTAMP)",
}


def _compile(config, time, expressions=(REVENUE, ORDERS)):
    return compile_query(
        config,
        Registry(config),
        {
            "select": [{"expression": expr, "as": f"v{i}"} for i, expr in enumerate(expressions)],
            "time": {"temporal_role": ROLE, "grain": "month", **time},
        },
    )


@pytest.mark.parametrize("warehouse", NOW)
@pytest.mark.parametrize("storage_zone", ["UTC", "America/New_York"])
def test_observation_and_coverage_render_for_every_dialect(
    package_config_factory, warehouse, storage_zone
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(
        config,
        aggregate_relations=[],
        package=replace(config.package, warehouse=warehouse),
        temporal_roles=[
            replace(r, timezone="America/New_York", column_timezone=storage_zone)
            if r.id == ROLE
            else r
            for r in config.temporal_roles
        ],
    )
    compiled = _compile(config, {"start": "2017-04-01", "end": "2017-05-01", "fill": True})
    nodes = list(sql_nodes(compiled["sql_ast"]))
    probes = [node.query for node in nodes if isinstance(node, SqlExists)]
    assert len(probes) == 2
    for probe in probes:
        sql = render_select(probe)
        assert "WHERE\n" in sql and "IS NOT NULL" in sql and "LIMIT 1" in sql
        assert not probe.having and not probe.group_by
    coverage = next(cte.query for cte in compiled["sql_ast"].ctes if cte.name == "coverage_1")
    sql = render_select(coverage)
    assert "MIN(" in sql and "MAX(CASE WHEN" in sql
    assert "AS loaded_from" in sql and "AS loaded_to" in sql
    assert NOW[warehouse] in sql
    # The clock has one conversion of the instant; column storage never wraps it again.
    assert [render_select(SqlSelect([field])) for field in coverage.select][-1].count(
        NOW[warehouse]
    ) == 1
    assert len(coverage.select) == 2
    guard = next(cte.query for cte in compiled["sql_ast"].ctes if cte.name == "guarded_base")
    assert all(
        isinstance(field.expression, SqlCall) and field.expression.name == "COALESCE"
        for field in guard.select
        if field.alias.startswith("m")
    )


@pytest.mark.parametrize(
    ("time", "coverage", "probes"),
    [
        ({}, False, 0),
        ({"start": "2017-04-01"}, False, 2),
        ({"end": "2017-05-01"}, False, 2),
        ({"fill": True}, True, 0),
        ({"start": "2017-04-01", "end": "2017-05-01", "fill": True}, True, 2),
    ],
)
def test_extra_reads_are_emitted_only_where_they_can_change_results(
    package_config_factory, time, coverage, probes
):
    config, _ = package_config_factory("jaffle_shop")
    compiled = _compile(config, time)
    assert any(cte.name.startswith("coverage_") for cte in compiled["sql_ast"].ctes) is coverage
    assert sum(isinstance(n, SqlExists) for n in sql_nodes(compiled["sql_ast"])) == probes
    if not coverage and not probes:
        assert compiled["sql"].count("FROM jaffle_order") == 1


def test_an_unrecognized_observation_aggregate_refuses(package_config_factory, monkeypatch):
    config, _ = package_config_factory("jaffle_shop")
    original = sql_lowering.record_leaf_scope

    def invalid(alias, scope):
        original(alias, replace(scope, value=SqlCall("AVG", [scope.raw_time])))

    monkeypatch.setattr(sql_lowering, "record_leaf_scope", invalid)
    with pytest.raises(SemanticLayerError) as caught:
        _compile(config, {"start": "2017-04-01"})
    assert caught.value.code == "EMPTY_GROUPS_UNSETTLED"
