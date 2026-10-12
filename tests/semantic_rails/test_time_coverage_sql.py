"""Coverage SQL portability and the boundaries where extra reads can change an answer."""

from dataclasses import replace

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import lowering_common, sql_lowering
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
# Warehouses that read coverage for a named window's every-period series, its probes read
# through CTEs, and an instant spelled in UTC by converting its declared storage zone.
NAMED_ONLY = {"snowflake", "bigquery", "databricks", "athena"}
CUTOFF = {
    "duckdb": "CAST('2026-10-01 00:00:00' AS TIMESTAMP)",
    "postgres": "CAST('2026-10-01 00:00:00' AS TIMESTAMP)",
    "snowflake": "CAST('2026-10-01 00:00:00' AS TIMESTAMP_NTZ)",
    "bigquery": "'2026-10-01 00:00:00'",
    "databricks": "CAST('2026-10-01 00:00:00' AS TIMESTAMP)",
    "athena": "CAST('2026-10-01 00:00:00' AS TIMESTAMP)",
}
NOW = "2026-10-01T00:00:00Z"


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
            "policy_context": {"now": NOW},
        },
    )


def _cte(compiled, name):
    nodes = sql_nodes(compiled["sql_ast"])
    return next(n.query for n in nodes if isinstance(n, SqlCte) and n.name == name)


FILLED = ((REVENUE,), {**BOUNDS, "fill": True})
COMBINED = ((REVENUE, ITEMS), BOUNDS)
SHAPES = [
    (zone, storage, *shape)
    for shape in (FILLED, COMBINED)
    for zone, storage in (("UTC", ""), ("America/New_York", "UTC"))
]


# DuckDB runs every zone and shape; each other dialect one of them.
@pytest.mark.parametrize(
    ("warehouse", "zone", "storage", "expressions", "time"),
    [("duckdb", *shape) for shape in SHAPES]
    + [(warehouse, *SHAPES[(i + 2) % 4]) for i, warehouse in enumerate(WAREHOUSES[1:])],
)
def test_coverage_renders_where_a_named_window_reads_it(
    package_config_factory, warehouse, zone, storage, expressions, time
):
    # BOUNDS is a named window under a grain: every period it holds, 0 inside the data.
    config = _config(package_config_factory, warehouse, zone, storage)
    compiled = _compile(config, time, expressions)
    # A bounded single-measure query never settles from coverage: its leaf is main's.
    leaf = render_select(_cte(compiled, "leaf_1"))
    assert leaf == render_select(_cte(_compile(config, BOUNDS, (REVENUE,)), "leaf_1"))
    assert "TYPEOF" not in leaf and "NOW(" not in leaf
    covered = warehouse in COVERED | NAMED_ONLY
    exists = any(isinstance(n, SqlExists) for n in sql_nodes(compiled["sql_ast"]))
    assert exists is (warehouse in COVERED)
    assert ("observed_1_rows" in compiled["sql"]) is (warehouse in NAMED_ONLY)
    assert ("coverage_" in compiled["sql"]) is covered
    assert ("TYPEOF" in compiled["sql"]) is (warehouse in COVERED)
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
    lowest, highest, through = (field.expression for field in coverage.select)
    bucket = lowest.args[0]
    assert f"{render_expr(bucket)} AS t" in leaf  # MIN/MAX of the leaf's own bucket
    cutoff = highest.args[0].whens[0]
    assert cutoff.result == bucket
    # The cutoff is the request's now, never the warehouse clock; the last row's instant
    # under it is the data-through edge.
    condition = render_expr(cutoff.condition)
    assert ("PG_TYPEOF" in condition) is (warehouse in COVERED) and "NOW()" not in condition
    assert CUTOFF[warehouse] in condition
    assert through.args[0].whens[0].condition == cutoff.condition
    assert all(isinstance(m, SqlCall) and m.name == "COALESCE" for m in measures)


def test_a_scope_on_a_warehouse_without_coverage_refuses(package_config_factory, monkeypatch):
    config = _config(package_config_factory, "clickhouse")
    for module in (lowering_common, sql_lowering):
        monkeypatch.setattr(module, "_needs_time_scope", lambda plan, config: True)
    with pytest.raises(SemanticLayerError) as caught:
        _compile(config, {"start": "2017-04-01"}, (REVENUE,))
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
                dialect=dialect_for_warehouse("clickhouse"),
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
    original = lowering_common.record_leaf_scope

    def invalid(alias, scope):
        original(alias, replace(scope, value=SqlCall("AVG", [scope.raw_time])))

    monkeypatch.setattr(lowering_common, "record_leaf_scope", invalid)
    with pytest.raises(SemanticLayerError) as caught:
        _compile(config, {"start": "2017-04-01"})
    assert caught.value.code == "EMPTY_GROUPS_UNSETTLED"


@pytest.mark.parametrize(
    ("storage_zone", "column_type", "hours"),
    [
        ("UTC", "TIMESTAMP", -1),
        ("America/New_York", "TIMESTAMP", 1),
        ("UTC", "TIMESTAMPTZ", 1),
        ("America/New_York", "TIMESTAMPTZ", -1),
    ],
)
def test_coverage_cutoff_compares_instants_in_a_different_session_zone(
    package_config_factory, storage_zone, column_type, hours
):
    config = _config(package_config_factory, zone="America/New_York", storage=storage_zone)
    compiled = _compile(config, {"fill": True}, (REVENUE,))
    coverage = _cte(compiled, "coverage_1")
    with duckdb.connect() as db:
        db.execute("SET TimeZone = 'America/Los_Angeles'")
        db.execute(f"CREATE TABLE jaffle_order (ordered_at {column_type})")
        stamp = f"TIMESTAMPTZ '{NOW}' + INTERVAL '{hours} hour'"
        if column_type == "TIMESTAMP":
            stamp = f"({stamp}) AT TIME ZONE '{storage_zone}'"
        db.execute(f"INSERT INTO jaffle_order VALUES ({stamp})")
        lo, hi, _ = db.execute(render_select(coverage)).fetchone()
        assert lo is not None
        assert (hi is not None) is (hours < 0)


UTC_STAMP = {
    "snowflake": "CONVERT_TIMEZONE('America/New_York', 'UTC', CAST(stamp AS TIMESTAMP_NTZ))",
    "bigquery": "DATETIME(TIMESTAMP(stamp, 'America/New_York'), 'UTC')",
    "databricks": "CONVERT_TIMEZONE('America/New_York', 'UTC', CAST(stamp AS TIMESTAMP))",
    "athena": (
        "CAST(AT_TIMEZONE(WITH_TIMEZONE(CAST(stamp AS TIMESTAMP), 'America/New_York'), 'UTC') "
        "AS TIMESTAMP)"
    ),
}


@pytest.mark.parametrize("warehouse", WAREHOUSES)
def test_only_duckdb_and_postgres_have_time_coverage(warehouse):
    dialect = dialect_for_warehouse(warehouse)
    assert dialect.has_time_coverage is (warehouse in COVERED)
    assert dialect.reads_coverage is (warehouse in COVERED | NAMED_ONLY)
    stamp = SqlIdentifier(["stamp"])
    if dialect.has_time_coverage:
        sql = render_expr(dialect.utc_timestamp(stamp, "America/New_York"))
        assert "PG_TYPEOF(stamp)" in sql and "CAST(stamp AS TIMESTAMPTZ)" in sql
        assert "'UTC'" in sql and "'America/New_York'" in sql
    elif dialect.reads_coverage:
        # Naive storage in its declared zone, converted to UTC; UTC storage reads as stored.
        assert render_expr(dialect.utc_timestamp(stamp, "America/New_York")) == UTC_STAMP[warehouse]
        assert dialect.utc_timestamp(stamp, "UTC") == dialect.timestamp_cast(stamp)


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


@pytest.mark.parametrize(
    ("warehouse", "zone"),
    [(warehouse, ["UTC", "Not/AZone"][i % 2]) for i, warehouse in enumerate(WAREHOUSES)],
)
def test_a_named_window_lists_every_period_or_says_it_shows_only_some(
    package_config_factory, warehouse, zone
):
    """Never silently sparse: every period from the day spine bounded by loaded coverage, or the
    observed periods with an OBSERVED_PERIODS_ONLY assumption naming why."""
    from semantic_rails.runtime import Runtime

    config, path = package_config_factory("jaffle_shop")
    config = _config(package_config_factory, warehouse, zone)
    runtime = Runtime.from_config(config, source_path=str(path), package_id="jaffle_shop")
    try:
        call = {"select": ["measure.jaffle.order_count"], "time": {**BOUNDS, "grain": "day"}}
        compiled = runtime.compile(call, now=NOW, verbosity="full")
    finally:
        runtime.close()
    every = warehouse in COVERED | NAMED_ONLY and zone == "UTC"
    assert ("implicit_days" in compiled["rendered_sql"]) is every
    assert ("coverage_1" in compiled["rendered_sql"]) is every
    observed = [a for a in compiled.get("assumptions", []) if a["code"] == "OBSERVED_PERIODS_ONLY"]
    if every:
        assert observed == []
        return
    reason = (
        f"warehouse '{warehouse}' can't list a window's empty periods"
        if warehouse not in COVERED | NAMED_ONLY
        else f"the clock's zone '{zone}' is not a known time zone"
    )
    assert observed == [
        {
            "code": "OBSERVED_PERIODS_ONLY",
            "message": f"The window's empty periods are not filled because {reason}: only "
            "periods with data are shown.",
            "reason": reason,
        }
    ]
