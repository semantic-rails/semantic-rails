"""The default calendar buckets like the implicit Gregorian one; any other calendar refuses.

An authored default calendar's period columns and ``date_day`` take no part in bucketing or
filling: the spine and the leaf both use the engine's ``DATE_TRUNC``. A non-default calendar,
or a grain on a clock bound to one, refuses where the query's calendar resolves, before any
SQL runs, whatever the leaf path or fill.
"""

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from semantic_rails.compiler import compile_query, lower_to_sql
from semantic_rails.compiler_parts.sql_lowering import _calendar_fill_binding
from semantic_rails.config import load_package_config
from semantic_rails.db import Database, DuckDBAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SeedSpec

ROOT = Path(__file__).resolve().parents[2] / "configs/semantic_rails/jaffle_shop"
ROLE = "temporal_role.jaffle_order_time"
ORDERS = {"expression": {"measure": "measure.jaffle.order_count"}, "as": "value"}
CALENDAR = "entity.jaffle_time"


def _calendar_runtime(adapter, zone="UTC", warehouse="duckdb", declared="date"):
    config = load_package_config(str(ROOT))
    config = replace(
        config,
        package=replace(config.package, warehouse=warehouse, seed=SeedSpec()),
        temporal_roles=[
            replace(row, timezone=zone) if row.id == ROLE else row for row in config.temporal_roles
        ],
        dimensions=[
            replace(row, data_type=declared)
            if row.entity == CALENDAR and row.column in {"date_day", "week_start"}
            else row
            for row in config.dimensions
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
    runtime = Runtime.from_config(config, source_path=str(ROOT))
    runtime.set_adapter(adapter)
    return runtime


def _duckdb_runtime(**options):
    adapter = DuckDBAdapter.__new__(DuckDBAdapter)
    adapter._db = Database.connect_in_memory()
    return _calendar_runtime(adapter, **options)


@pytest.fixture
def calendar_runtime(request):
    runtime = _duckdb_runtime(zone=getattr(request, "param", "UTC"))
    try:
        yield runtime
    finally:
        runtime.close()


def _query_buckets(runtime, *, bounded=True, wide=False, revenue=False, grain="week", window=None):
    time = {"temporal_role": ROLE, "grain": grain, "fill": True}
    if window:
        time.update(start=window[0], end=window[1])
    elif bounded:
        time.update(
            start="2024-04-29" if wide else "2024-05-06", end="2024-06-03" if wide else "2024-05-20"
        )
    select = {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "value"}
    rows = runtime.query({"version": 1, "select": [select if revenue else ORDERS], "time": time})[
        "rows"
    ]
    alias = f"{ROLE}__{grain}"
    return [(datetime.fromisoformat(str(row[alias])).date(), row["value"]) for row in rows]


def _reference(adapter, *, first="2024-05-06", last="2024-05-13", revenue=False):
    # Independently group source rows by DATE_TRUNC, then add explicit weekly buckets.
    # Coverage comes from the source, never from the calendar or the compiled query.
    aggregate = "SUM(o.order_total_cents / 100.0)" if revenue else "COUNT(o.order_id)"
    rows = adapter.query(
        f"WITH counts AS (SELECT DATE_TRUNC('week', o.ordered_at) AS bucket, {aggregate} AS n "
        "FROM jaffle_order o GROUP BY 1), weeks AS (SELECT d AS bucket FROM generate_series("
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


@pytest.mark.parametrize("declared", ["date", "timestamp"])
@pytest.mark.parametrize("anchor", ["date", "noon", "sunday"])
@pytest.mark.parametrize("bounded", [True, False])
def test_default_fill_ignores_authored_week_anchor(declared, anchor, bounded):
    # A Sunday-week (or noon) authored default calendar still answers ISO weeks.
    runtime = _duckdb_runtime(declared=declared)
    adapter = runtime._get_adapter()
    try:
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
                adapter.query(
                    "UPDATE jaffle_calendar SET date_day = date_day + INTERVAL '12 hours'"
                )
        expected = _reference(adapter, last="2024-05-13" if bounded else "2024-05-20")
        assert _query_buckets(runtime, bounded=bounded) == expected
    finally:
        runtime.close()
    assert expected[:2] == [(date(2024, 5, 6), 2), (date(2024, 5, 13), 0)]


@pytest.mark.parametrize("physical", ["TIMESTAMP", "TIMESTAMPTZ"])
@pytest.mark.parametrize(
    ("grain", "bucket"), [("day", date(2024, 5, 7)), ("week", date(2024, 5, 6))]
)
def test_noon_day_keys_keep_an_intraday_window(calendar_runtime, physical, grain, bucket):
    # The order at May 7 noon falls inside [11:00, 13:00); noon calendar days don't move it.
    adapter = calendar_runtime._get_adapter()
    adapter.query(f"ALTER TABLE jaffle_calendar ALTER COLUMN date_day SET DATA TYPE {physical}")
    adapter.query("UPDATE jaffle_calendar SET date_day = date_day + INTERVAL '12 hours'")
    window = ("2024-05-07T11:00:00", "2024-05-07T13:00:00")
    reference = adapter.query(
        f"SELECT DATE_TRUNC('{grain}', ordered_at) AS b, COUNT(order_id) AS n "
        f"FROM jaffle_order WHERE ordered_at >= TIMESTAMP '{window[0]}' "
        f"AND ordered_at < TIMESTAMP '{window[1]}' GROUP BY 1"
    )

    assert [(row["b"].date(), row["n"]) for row in reference] == [(bucket, 1)]
    assert _query_buckets(calendar_runtime, grain=grain, window=window) == [(bucket, 1)]


@pytest.mark.parametrize("revenue", [False, True])
def test_fill_preserves_observed_zero_and_absent_coverage(calendar_runtime, revenue):
    expected = _reference(
        calendar_runtime._get_adapter(), first="2024-04-29", last="2024-05-27", revenue=revenue
    )
    assert [value for _, value in expected] == [None, 0 if revenue else 2, 0, 1, None]
    assert _query_buckets(calendar_runtime, wide=True, revenue=revenue) == expected


def test_an_authored_default_calendar_never_binds_the_fill():
    config = load_package_config(str(ROOT))
    query = {"version": 1, "select": [ORDERS], "time": {"temporal_role": ROLE, "grain": "week"}}
    plan = compile_query(config, Registry(config), query)["logical_plan"]
    for bounds, day in (({}, None), ({"start": "2024-05-06", "end": "2024-05-20"}, "date_day")):
        filled = replace(plan, time={**plan.time, **bounds, "fill": True})
        assert _calendar_fill_binding(filled, config) == ("implicit_calendar", "bucket", day)


def _bound_to_fiscal(config):
    return replace(
        config,
        entities=[
            replace(row, calendar_id="fiscal") if row.id == "entity.jaffle_order" else row
            for row in config.entities
        ],
    )


ENTITY_IN_TERMS_OF = {"group_by": ["dimension.jaffle_item_product_type"]}
WINDOW = {"start": "2017-02-01", "end": "2017-08-01"}


@pytest.mark.parametrize(
    ("bound", "time", "shape"),
    [
        (False, {"grain": "quarter", "calendar_id": "fiscal", "fill": True}, {}),
        (False, {"grain": "quarter", "calendar_id": "fiscal", "fill": False}, {}),
        (False, {"grain": "week", "calendar_id": "Fiscal", "fill": True, **WINDOW}, {}),
        (False, {"calendar_id": "fiscal", **WINDOW}, {}),
        (False, {"grain": "quarter", "calendar_id": "fiscal", "fill": True}, ENTITY_IN_TERMS_OF),
        (False, {"grain": "quarter", "calendar_id": "fiscal"}, ENTITY_IN_TERMS_OF),
        (True, {"grain": "quarter", "fill": False}, {}),
        (True, {"grain": "week", "fill": True, **WINDOW}, {}),
        (True, {"grain": "quarter", "calendar_id": "default"}, ENTITY_IN_TERMS_OF),
    ],
    ids=[
        "fiscal-filled",
        "fiscal-unfilled",
        "fiscal-mixed-case",
        "fiscal-no-grain",
        "fiscal-entity-in-terms-of-filled",
        "fiscal-entity-in-terms-of",
        "bound-unfilled",
        "bound-filled",
        "bound-entity-in-terms-of",
    ],
)
def test_a_non_default_calendar_refuses_on_every_leaf_path(bound, time, shape):
    config = load_package_config(str(ROOT))
    config = _bound_to_fiscal(config) if bound else config
    query = {"version": 1, "select": [ORDERS], "time": {"temporal_role": ROLE, **time}, **shape}
    with pytest.raises(SemanticLayerError) as refused:
        compile_query(config, Registry(config), query)  # compiles no SQL, so none runs

    assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    assert refused.value.details["reason"] == "calendar_not_supported_yet"
    assert refused.value.details["calendar_id"] == "fiscal"
    assert "fiscal calendars return in a later release" in str(refused.value)


def test_a_bound_clock_without_a_grain_does_not_bucket():
    config = _bound_to_fiscal(load_package_config(str(ROOT)))
    query = {"version": 1, "select": [ORDERS], "time": {"temporal_role": ROLE, **WINDOW}}

    assert compile_query(config, Registry(config), query)["sql"]


@pytest.mark.parametrize("bypass", ["calendar_id", "bound_clock"])
def test_lowering_refuses_a_plan_that_skipped_validation(bypass):
    config = load_package_config(str(ROOT))
    query = {
        "version": 1,
        "select": [ORDERS],
        "time": {"temporal_role": ROLE, "grain": "quarter", "fill": True},
    }
    plan = compile_query(config, Registry(config), query)["logical_plan"]
    assert lower_to_sql(plan, config)  # the default plan lowers
    if bypass == "calendar_id":
        plan = replace(plan, time={**plan.time, "calendar_id": "fiscal"})
    else:
        config = _bound_to_fiscal(config)
    with pytest.raises(SemanticLayerError) as refused:
        lower_to_sql(plan, config)

    assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    assert refused.value.details["reason"] == "calendar_not_supported_yet"
