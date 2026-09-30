"""Coverage SQL portability and the boundaries where extra reads can change an answer."""

from dataclasses import replace

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.compiler_parts.empty_groups import sql_nodes
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_select
from semantic_rails.sql_ast import SqlCall, SqlExists, SqlSelect

ROLE = "temporal_role.jaffle_order_time"
REVENUE = {"measure": "measure.jaffle.revenue_usd"}
ORDERS = {"measure": "measure.jaffle.order_count"}
NOW = {
    "duckdb": "TIMEZONE('UTC', NOW())",
    "postgres": "TIMEZONE('UTC', NOW())",
    "snowflake": "CAST(CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP()) AS TIMESTAMP_NTZ)",
    "bigquery": "CURRENT_DATETIME('UTC')",
    "databricks": "CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())",
    "clickhouse": "NOW('UTC')",
    "athena": "CAST(AT_TIMEZONE(NOW(), 'UTC') AS TIMESTAMP)",
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


@pytest.mark.parametrize("storage_zone", ["UTC", "America/New_York"])
@pytest.mark.parametrize("column_type", ["TIMESTAMP", "TIMESTAMPTZ"])
@pytest.mark.parametrize("hours", [-1, 1], ids=["past", "future"])
def test_coverage_cutoff_compares_instants_in_a_different_session_zone(
    package_config_factory, storage_zone, column_type, hours
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(
        config,
        aggregate_relations=[],
        temporal_roles=[
            replace(r, timezone="America/New_York", column_timezone=storage_zone)
            if r.id == ROLE
            else r
            for r in config.temporal_roles
        ],
    )
    compiled = _compile(config, {"fill": True}, (REVENUE,))
    coverage = next(cte.query for cte in compiled["sql_ast"].ctes if cte.name == "coverage_1")
    with duckdb.connect() as db:
        db.execute("SET TimeZone = 'America/Los_Angeles'")
        db.execute(f"CREATE TABLE jaffle_order (ordered_at {column_type})")
        stamp = f"CURRENT_TIMESTAMP + INTERVAL '{hours} hour'"
        if column_type == "TIMESTAMP":
            stamp = f"({stamp}) AT TIME ZONE '{storage_zone}'"
        db.execute(f"INSERT INTO jaffle_order VALUES ({stamp})")
        lo, hi = db.execute(render_select(coverage)).fetchone()
        assert lo is not None
        assert (hi is not None) is (hours < 0)


@pytest.mark.parametrize("warehouse", NOW)
def test_cutoff_normalizes_storage_to_utc_for_each_dialect(warehouse):
    from semantic_rails.sql_ast import SqlField, SqlIdentifier

    dialect = dialect_for_warehouse(warehouse)
    sql = render_select(
        SqlSelect(
            [SqlField(dialect.utc_timestamp(SqlIdentifier(["stamp"]), "America/New_York"), "v")]
        )
    )
    assert "'UTC'" in sql and "'America/New_York'" in sql
    if warehouse in {"duckdb", "postgres"}:
        assert "PG_TYPEOF(stamp)" in sql and "CAST(stamp AS TIMESTAMPTZ)" in sql


@pytest.mark.parametrize("role_zone", ["UTC", "America/New_York"])
@pytest.mark.parametrize("storage_zone", ["UTC", "America/New_York"])
@pytest.mark.parametrize("column_type", ["TIMESTAMP", "TIMESTAMPTZ"])
def test_coverage_buckets_use_the_role_frame(
    package_config_factory, role_zone, storage_zone, column_type
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(
        config,
        aggregate_relations=[],
        temporal_roles=[
            replace(r, timezone=role_zone, column_timezone=storage_zone) if r.id == ROLE else r
            for r in config.temporal_roles
        ],
    )
    compiled = _compile(config, {"fill": True}, (REVENUE,))
    coverage = next(cte.query for cte in compiled["sql_ast"].ctes if cte.name == "coverage_1")
    with duckdb.connect() as db:
        db.execute("SET TimeZone = 'America/Los_Angeles'")
        db.execute(f"CREATE TABLE jaffle_order (ordered_at {column_type})")
        stamp = "TIMESTAMPTZ '2024-03-01 06:00:00+00'"
        if column_type == "TIMESTAMP":
            stamp = f"({stamp}) AT TIME ZONE '{storage_zone}'"
        db.execute(f"INSERT INTO jaffle_order VALUES ({stamp})")
        lo, hi = db.execute(render_select(coverage)).fetchone()
        assert str(lo).startswith("2024-03-01") and hi == lo


def test_scope_reads_report_unbounded_large_scans(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(
        config,
        aggregate_relations=[],
        entities=[
            replace(e, table="GMV_FEED_POWDERHORN") if e.table == "jaffle_order" else e
            for e in config.entities
        ],
    )
    compiled = _compile(
        config, {"start": "2017-04-01", "end": "2017-05-01", "fill": True}, (REVENUE,)
    )
    performance = compiled["explain"].performance_plan
    reads = performance["estimated_scan_relations"]
    assert len(reads) == 3
    assert all(r["relation"] == "GMV_FEED_POWDERHORN" for r in reads)
    assert [(r["time_filter_has_start"], r["time_filter_has_end"]) for r in reads] == [
        (True, True),
        (False, False),
        (False, False),
    ]
    assert performance["large_scan_risk"]["all_raw_scans_bounded"] is False
    assert performance["risk_level"] == "high"
    assert performance["execution_recommendation"] == "needs_acceleration_layer"
