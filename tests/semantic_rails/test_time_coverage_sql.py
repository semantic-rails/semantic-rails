"""Coverage SQL portability and the boundaries where extra reads can change an answer."""

from dataclasses import replace

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.compiler_parts.empty_groups import LeafScope, guard_empty_groups, sql_nodes
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr, render_select
from semantic_rails.sql_ast import (
    SqlCall,
    SqlCase,
    SqlCte,
    SqlExists,
    SqlIdentifier,
    SqlTableRef,
)

ROLE = "temporal_role.jaffle_order_time"
REVENUE = {"measure": "measure.jaffle.revenue_usd"}
ORDERS = {"measure": "measure.jaffle.order_count"}
ITEMS = {"measure": "measure.jaffle.item_count"}
BOUNDS = {"start": "2017-04-01", "end": "2017-05-01"}
WAREHOUSES = ("duckdb", "postgres", "snowflake", "bigquery", "databricks", "clickhouse", "athena")
COVERED = {"duckdb", "postgres"}  # the warehouses whose coverage SQL CI executes


def _config(factory, warehouse="duckdb", zone="UTC", storage=""):
    config, _ = factory("jaffle_shop")
    return replace(
        config,
        aggregate_relations=[],
        package=replace(config.package, warehouse=warehouse),
        temporal_roles=[
            replace(r, timezone=zone, column_timezone=storage) if r.id == ROLE else r
            for r in config.temporal_roles
        ],
    )


def _compile(config, time, expressions=(REVENUE, ORDERS)):
    return compile_query(
        config,
        Registry(config),
        {
            "select": [{"expression": expr, "as": f"v{i}"} for i, expr in enumerate(expressions)],
            "time": {"temporal_role": ROLE, "grain": "month", **time},
        },
    )


def _cte(compiled, name):
    nodes = sql_nodes(compiled["sql_ast"])
    return next(n.query for n in nodes if isinstance(n, SqlCte) and n.name == name)


@pytest.mark.parametrize("warehouse", WAREHOUSES)
@pytest.mark.parametrize(("zone", "storage"), [("UTC", ""), ("America/New_York", "UTC")])
@pytest.mark.parametrize(
    ("expressions", "time"),
    [((REVENUE,), {**BOUNDS, "fill": True}), ((REVENUE, ITEMS), BOUNDS)],
    ids=["filled", "combined"],
)
def test_coverage_renders_only_where_ci_executes_it(
    package_config_factory, warehouse, zone, storage, expressions, time
):
    config = _config(package_config_factory, warehouse, zone, storage)
    if time.get("fill") and not dialect_for_warehouse(warehouse).has_implicit_calendar:
        # Every fill uses the implicit calendar; an authored one takes no part.
        with pytest.raises(SemanticLayerError, match="no implicit calendar"):
            _compile(config, time, expressions)
        return
    compiled = _compile(config, time, expressions)
    # A bounded single-measure query never settles from coverage: its leaf is main's.
    leaf = render_select(_cte(compiled, "leaf_1"))
    assert leaf == render_select(_cte(_compile(config, BOUNDS, (REVENUE,)), "leaf_1"))
    assert "TYPEOF" not in leaf and "NOW(" not in leaf
    covered = warehouse in COVERED
    assert any(isinstance(n, SqlExists) for n in sql_nodes(compiled["sql_ast"])) is covered
    assert ("coverage_" in compiled["sql"]) is covered
    assert ("TYPEOF" in compiled["sql"]) is covered
    guard = _cte(compiled, "guarded_base")
    measures = [f.expression for f in guard.select if f.alias.startswith("m")]
    # The revenue sum keeps its value and fills only a group with no rows:
    # COALESCE(value, CASE WHEN <seen> AND <no rows> THEN 0 END).
    revenue, *counts = measures
    assert isinstance(revenue, SqlCall) and revenue.name == "COALESCE"
    assert isinstance(revenue.args[1], SqlCase)
    if not covered:
        # Main's in-window test: CASE WHEN <seen in the window> THEN COALESCE(value, 0) END.
        assert all(isinstance(m, SqlCase) for m in counts)
        return
    coverage = _cte(compiled, "coverage_1")
    lowest, highest = (field.expression for field in coverage.select)
    bucket = lowest.args[0]
    assert f"{render_expr(bucket)} AS t" in leaf  # MIN/MAX of the leaf's own bucket
    cutoff = highest.args[0].whens[0]
    assert cutoff.result == bucket
    assert "PG_TYPEOF" in render_expr(cutoff.condition) and "NOW()" in render_expr(cutoff.condition)
    assert all(isinstance(m, SqlCall) and m.name == "COALESCE" for m in measures)


def test_a_scope_on_a_warehouse_without_coverage_refuses(package_config_factory, monkeypatch):
    config = _config(package_config_factory, "snowflake")
    monkeypatch.setattr(sql_lowering, "_needs_time_scope", lambda plan, config: True)
    with pytest.raises(SemanticLayerError) as caught:
        _compile(config, {**BOUNDS, "fill": True}, (REVENUE,))
    assert caught.value.code == "EMPTY_GROUPS_UNSETTLED"
    scope = LeafScope(SqlTableRef("t"), (), (), SqlCall("SUM", [SqlIdentifier(["t", "v"])]))
    for scopes, time_key in (({"m": scope}, ""), ({}, "t")):
        with pytest.raises(SemanticLayerError):
            guard_empty_groups(
                "base",
                ["t"],
                ["m"],
                {"m": "sum"},
                scopes,
                time_key=time_key,
                dialect=dialect_for_warehouse("snowflake"),
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
    config = _config(package_config_factory, zone="America/New_York", storage=storage_zone)
    compiled = _compile(config, {"fill": True}, (REVENUE,))
    coverage = _cte(compiled, "coverage_1")
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


@pytest.mark.parametrize("warehouse", WAREHOUSES)
def test_only_duckdb_and_postgres_have_time_coverage(warehouse):
    dialect = dialect_for_warehouse(warehouse)
    assert dialect.has_time_coverage is (warehouse in COVERED)
    if dialect.has_time_coverage:
        sql = render_expr(dialect.utc_timestamp(SqlIdentifier(["stamp"]), "America/New_York"))
        assert "PG_TYPEOF(stamp)" in sql and "CAST(stamp AS TIMESTAMPTZ)" in sql
        assert "'UTC'" in sql and "'America/New_York'" in sql


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
