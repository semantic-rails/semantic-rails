"""Calendar fills preserve the leaf's buckets and their coverage semantics."""

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.sql_lowering import _calendar_fill_binding
from semantic_rails.config import load_package_config
from semantic_rails.db import Database, DuckDBAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SeedSpec

ROLE = "temporal_role.jaffle_order_time"
ALIAS = f"{ROLE}__week"


def _calendar_runtime(adapter, zone="UTC", warehouse="duckdb"):
    root = Path(__file__).resolve().parents[2] / "configs/semantic_rails/jaffle_shop"
    config = load_package_config(str(root))
    config = replace(
        config,
        package=replace(config.package, warehouse=warehouse, seed=SeedSpec()),
        temporal_roles=[
            replace(row, timezone=zone) if row.id == ROLE else row for row in config.temporal_roles
        ],
    )
    adapter.query("SET TimeZone='UTC'")
    table = "TABLE" if warehouse == "duckdb" else "TEMP TABLE"
    adapter.query(
        f"CREATE {table} jaffle_order AS SELECT * FROM (VALUES "
        "('one', TIMESTAMP '2024-05-07 12:00:00', 0), "
        "('two', TIMESTAMP '2024-05-08 12:00:00', 0), "
        "('later', TIMESTAMP '2024-05-25 12:00:00', 100)) "
        "AS orders(order_id, ordered_at, order_total_cents)"
    )
    adapter.query(
        f"CREATE {table} jaffle_calendar AS SELECT CAST(d AS DATE) AS date_day, "
        "CAST(DATE_TRUNC('week', d) AS DATE) AS week_start "
        "FROM generate_series(TIMESTAMP '2024-04-29', TIMESTAMP '2024-06-02', "
        "INTERVAL '1 day') AS days(d)"
    )
    adapter.query(f"CREATE {table} jaffle_calendar_fiscal AS SELECT * FROM jaffle_calendar")
    runtime = Runtime.from_config(config, source_path=str(root))
    runtime.set_adapter(adapter)
    return runtime


@pytest.fixture
def calendar_runtime(request):
    adapter = DuckDBAdapter.__new__(DuckDBAdapter)
    adapter._db = Database.connect_in_memory()
    runtime = _calendar_runtime(adapter, zone=getattr(request, "param", "UTC"))
    try:
        yield runtime
    finally:
        runtime.close()


def _query_buckets(runtime, *, calendar="default", bounded=True, wide=False, revenue=False):
    time = {"temporal_role": ROLE, "grain": "week", "fill": True, "calendar_id": calendar}
    if bounded:
        time.update(
            start="2024-04-29" if wide else "2024-05-06", end="2024-06-03" if wide else "2024-05-20"
        )
    rows = runtime.query(
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "measure": "measure.jaffle.revenue_usd"
                        if revenue
                        else "measure.jaffle.order_count"
                    },
                    "as": "value",
                }
            ],
            "time": time,
        }
    )["rows"]
    return [(datetime.fromisoformat(str(row[ALIAS])).date(), row["value"]) for row in rows]


def _reference(adapter, *, first="2024-05-06", last="2024-05-13", fiscal=False, revenue=False):
    # Independently group source rows, then add explicit weekly buckets. Coverage
    # comes from the source, never from the calendar or the compiled query.
    bucket = "c.week_start" if fiscal else "DATE_TRUNC('week', o.ordered_at)"
    join = (
        "JOIN jaffle_calendar_fiscal c ON CAST(c.date_day AS DATE) = CAST(o.ordered_at AS DATE)"
        if fiscal
        else ""
    )
    aggregate = "SUM(o.order_total_cents / 100.0)" if revenue else "COUNT(o.order_id)"
    rows = adapter.query(
        f"WITH counts AS (SELECT {bucket} AS bucket, {aggregate} AS n FROM jaffle_order o "
        f"{join} GROUP BY 1), weeks AS (SELECT d AS bucket FROM generate_series("
        f"TIMESTAMP '{first}', TIMESTAMP '{last}', INTERVAL '7 days') AS days(d)) "
        "SELECT w.bucket, CASE WHEN w.bucket BETWEEN (SELECT MIN(bucket) FROM counts) "
        "AND (SELECT MAX(bucket) FROM counts) THEN CASE WHEN c.bucket IS NULL THEN 0 "
        "ELSE c.n END END AS n FROM weeks w LEFT JOIN counts c ON w.bucket = c.bucket "
        "ORDER BY w.bucket"
    )
    return [(row["bucket"].date(), row["n"]) for row in rows]


@pytest.mark.parametrize("physical", ["TIMESTAMP", "TIMESTAMPTZ"])
@pytest.mark.parametrize("shift", [0, 12, -1])
@pytest.mark.parametrize(
    "calendar_runtime", ["UTC", "America/Los_Angeles", "Pacific/Auckland"], indirect=True
)
@pytest.mark.parametrize("columns", [("week_start",), ("week_start", "date_day")])
def test_default_fill_uses_leaf_buckets(calendar_runtime, physical, shift, columns):
    runtime = calendar_runtime
    adapter = runtime._get_adapter()
    for column in columns:
        adapter.query(f"ALTER TABLE jaffle_calendar ALTER COLUMN {column} SET DATA TYPE {physical}")
        adapter.query(f"UPDATE jaffle_calendar SET {column} = {column} + INTERVAL '{shift} hours'")
    expected = [(date(2024, 5, 6), 2), (date(2024, 5, 13), 0)]
    assert _reference(adapter) == expected
    assert _query_buckets(runtime) == expected


@pytest.mark.parametrize("anchor", ["date", "noon", "sunday"])
@pytest.mark.parametrize("bounded", [True, False])
def test_default_fill_ignores_authored_week_anchor(calendar_runtime, anchor, bounded):
    adapter = calendar_runtime._get_adapter()
    if anchor != "date":
        if anchor == "noon":
            adapter.query(
                "ALTER TABLE jaffle_calendar ALTER COLUMN week_start SET DATA TYPE TIMESTAMP"
            )
        adapter.query(
            "UPDATE jaffle_calendar SET week_start = week_start + INTERVAL '12 hours'"
            if anchor == "noon"
            else "UPDATE jaffle_calendar SET week_start = week_start - INTERVAL '1 day'"
        )
        if anchor == "noon":
            adapter.query(
                "ALTER TABLE jaffle_calendar ALTER COLUMN date_day SET DATA TYPE TIMESTAMP"
            )
            adapter.query("UPDATE jaffle_calendar SET date_day = date_day + INTERVAL '12 hours'")
    expected = _reference(adapter, last="2024-05-13" if bounded else "2024-05-20")
    assert _query_buckets(calendar_runtime, bounded=bounded) == expected
    assert expected[:2] == [(date(2024, 5, 6), 2), (date(2024, 5, 13), 0)]


@pytest.mark.parametrize("noon_day", [False, True])
def test_fiscal_fill_keeps_authored_noon_buckets(calendar_runtime, noon_day):
    adapter = calendar_runtime._get_adapter()
    for column in ("week_start", "date_day") if noon_day else ("week_start",):
        adapter.query(
            f"ALTER TABLE jaffle_calendar_fiscal ALTER COLUMN {column} SET DATA TYPE TIMESTAMP"
        )
        adapter.query(
            f"UPDATE jaffle_calendar_fiscal SET {column} = {column} + INTERVAL '12 hours'"
        )
    expected = _reference(
        adapter, fiscal=True, first="2024-05-06 12:00:00", last="2024-05-13 12:00:00"
    )
    assert expected == [(date(2024, 5, 6), 2), (date(2024, 5, 13), 0)]
    assert _query_buckets(calendar_runtime, calendar="fiscal") == expected


@pytest.mark.parametrize("revenue", [False, True])
def test_fill_preserves_observed_zero_and_absent_coverage(calendar_runtime, revenue):
    expected = _reference(
        calendar_runtime._get_adapter(), first="2024-04-29", last="2024-05-27", revenue=revenue
    )
    assert [value for _, value in expected] == [None, 0 if revenue else 2, 0, 1, None]
    assert _query_buckets(calendar_runtime, wide=True, revenue=revenue) == expected


@pytest.mark.parametrize("path", ["runtime", "forced_fill", "missing_leaf_binding"])
def test_nondefault_fill_refuses_a_different_leaf_bucket(calendar_runtime, path):
    runtime = calendar_runtime
    adapter = runtime._get_adapter()
    adapter.query("UPDATE jaffle_calendar_fiscal SET week_start = week_start - INTERVAL '1 day'")
    expected = _reference(adapter, fiscal=True, first="2024-05-05", last="2024-05-12")
    assert expected == [(date(2024, 5, 5), 2), (date(2024, 5, 12), 0)]
    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "value"}],
        "time": {"temporal_role": ROLE, "grain": "week", "fill": True, "calendar_id": "fiscal"},
    }
    config = runtime.config
    plan = compile_query(config, Registry(config), query)["logical_plan"]
    if path != "missing_leaf_binding":
        config = replace(
            config,
            entities=[
                replace(row, calendar_id="fiscal") if row.id == "entity.jaffle_order" else row
                for row in config.entities
            ],
        )
    with patch.object(adapter, "query", wraps=adapter.query) as execute:
        with pytest.raises(SemanticLayerError) as refused:
            if path == "runtime":
                changed_runtime = Runtime.from_config(
                    config,
                    source_path=str(
                        Path(__file__).resolve().parents[2] / "configs/semantic_rails/jaffle_shop"
                    ),
                )
                changed_runtime.set_adapter(adapter)
                try:
                    _query_buckets(changed_runtime, calendar="fiscal")
                finally:
                    changed_runtime.close()
            elif path == "forced_fill":
                _calendar_fill_binding(
                    replace(plan, time={**plan.time, "fill": False}), config, force=True
                )
            else:
                with patch(
                    "semantic_rails.compiler_parts.sql_lowering._leaf_calendar_binding",
                    return_value=None,
                ):
                    _calendar_fill_binding(plan, config)
        execute.assert_not_called()
    assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    assert refused.value.details == {
        "reason": "calendar_leaf_unbound",
        "calendar_id": "fiscal",
        "temporal_role": ROLE,
    }
