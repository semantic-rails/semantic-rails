"""Role-local range resolution on normalization, nested compilation and cache hits."""

from dataclasses import replace
from datetime import UTC, date, datetime

import duckdb
import pytest

from semantic_rails import ast
from semantic_rails.compiler import _compile_query_sql_ast, compile_query
from semantic_rails.compiler_parts.sql_lowering import _apply_role_timezone
from semantic_rails.config import load_package_config
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata import _query_state
from semantic_rails.metadata_parts.valid_values import _query_state as values_query_state
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlCall, SqlIdentifier

ROLE = "temporal_role.jaffle_order_time"


@pytest.fixture
def config():
    original = load_package_config("configs/semantic_rails/jaffle_shop")
    return replace(
        original,
        temporal_roles=[replace(r, timezone="America/New_York") for r in original.temporal_roles],
    )


@pytest.mark.parametrize("warehouse", ["duckdb", "postgres"])
@pytest.mark.parametrize("session_zone", ["UTC", "America/New_York", "America/Los_Angeles"])
def test_converted_date_uses_a_naive_timestamp_input(config, warehouse, session_zone):
    role = replace(next(r for r in config.temporal_roles if r.id == ROLE), column_timezone="UTC")
    config = replace(
        config,
        package=replace(config.package, warehouse=warehouse),
        dimensions=[
            replace(d, data_type="date") if d.id == role.dimension else d for d in config.dimensions
        ],
    )
    converted = _apply_role_timezone(SqlIdentifier(parts=["source_day"]), role, config)
    assert isinstance(converted, SqlCall)
    source_conversion = converted.args[1]
    assert isinstance(source_conversion, SqlCall)
    source_input = render_expr(source_conversion.args[1])
    events = (
        "(VALUES (DATE '2024-07-01', 10), (DATE '2024-07-02', 20)) AS events(source_day, amount)"
    )
    with duckdb.connect(":memory:") as connection:
        connection.execute(f"SET TimeZone = '{session_zone}'")
        # Postgres can coerce a bare DATE to TIMESTAMPTZ for timezone().
        # Verify the emitted input type locally, where DuckDB's overload differs.
        assert connection.execute(
            f"SELECT TYPEOF({source_input}) FROM {events} LIMIT 1"
        ).fetchone() == ("TIMESTAMP",)
        actual = connection.execute(
            f"SELECT CAST({render_expr(converted)} AS DATE), amount FROM {events} ORDER BY 1"
        ).fetchall()
        reference = connection.execute(
            "SELECT CAST(((CAST(source_day AS TIMESTAMP) AT TIME ZONE 'UTC') "
            f"AT TIME ZONE 'America/New_York') AS DATE), amount FROM {events} ORDER BY 1"
        ).fetchall()
    assert actual == reference == [(date(2024, 6, 30), 10), (date(2024, 7, 1), 20)]


_NAIVE_DATE = "TIMEZONE('America/New_York', TIMEZONE('UTC', CAST(source_day AS TIMESTAMP)))"
# Only the warehouses whose converted DATE clocks are executed in tests cast the DATE;
# the others keep the conversion they emitted before that cast existed.
DATE_CLOCK_CONVERSIONS = {
    "athena": "AT_TIMEZONE(WITH_TIMEZONE(source_day, 'UTC'), 'America/New_York')",
    "bigquery": "DATETIME(TIMESTAMP(source_day, 'UTC'), 'America/New_York')",
    "clickhouse": "toTimeZone(toDateTime(source_day, 'UTC'), 'America/New_York')",
    "databricks": "CONVERT_TIMEZONE('UTC', 'America/New_York', source_day)",
    "duckdb": _NAIVE_DATE,
    "ducklake": _NAIVE_DATE,
    "motherduck": _NAIVE_DATE,
    "postgres": _NAIVE_DATE,
    "snowflake": "CONVERT_TIMEZONE('UTC', 'America/New_York', source_day)",
}


def test_date_clock_conversions_cover_every_warehouse():
    assert sorted(DATE_CLOCK_CONVERSIONS) == sorted(supported_warehouses())


@pytest.mark.parametrize("warehouse", sorted(DATE_CLOCK_CONVERSIONS))
def test_only_tested_warehouses_cast_a_converted_date_clock(config, warehouse):
    role = replace(next(r for r in config.temporal_roles if r.id == ROLE), column_timezone="UTC")
    config = replace(
        config,
        package=replace(config.package, warehouse=warehouse),
        dimensions=[
            replace(d, data_type="date") if d.id == role.dimension else d for d in config.dimensions
        ],
    )
    converted = _apply_role_timezone(SqlIdentifier(parts=["source_day"]), role, config)
    assert render_expr(converted) == DATE_CLOCK_CONVERSIONS[warehouse]


SESSION_ROLE = "temporal_role.jaffle_session_started_at"
SESSION_SEED = """
CREATE TABLE jaffle_customer AS SELECT * FROM (VALUES ('c1'), ('c2')) AS t(customer_id);
CREATE TABLE jaffle_storefront_session AS SELECT * FROM (VALUES
  ('s1', 'c1', DATE '2024-07-01'), ('s2', 'c1', DATE '2024-07-02'), ('s3', 'c2', DATE '2024-06-30')
) AS t(session_id, customer_id, started_at);
CREATE TABLE jaffle_order AS SELECT * FROM (VALUES
  ('o1', 'c1', TIMESTAMP '2024-07-01 12:00:00')
) AS t(order_id, customer_id, ordered_at);
"""
# Base sessions in the window's whole days; converted when the customer orders within 7 days.
SESSION_REFERENCE = """
SELECT CAST(s.started_at AS TIMESTAMP), AVG(CASE WHEN EXISTS (
  SELECT 1 FROM jaffle_order o WHERE o.customer_id = s.customer_id
    AND o.ordered_at >= s.started_at AND o.ordered_at < s.started_at + INTERVAL 7 DAY
) THEN 1.0 ELSE 0.0 END)
FROM jaffle_storefront_session s
WHERE s.started_at >= DATE '2024-06-30' AND s.started_at <= DATE '2024-07-01'
GROUP BY 1 ORDER BY 1
"""


def _session_conversion(config, data_type, column_timezone):
    """A daily session-to-order conversion with the session clock's type and storage zone."""
    role = next(r for r in config.temporal_roles if r.id == SESSION_ROLE)
    config = replace(
        config,
        temporal_roles=[
            replace(r, column_timezone=column_timezone) if r.id == SESSION_ROLE else r
            for r in config.temporal_roles
        ],
        dimensions=[
            replace(d, data_type=data_type) if d.id == role.dimension else d
            for d in config.dimensions
        ],
    )
    conversion = {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 7},
        "matching_mode": "first_converted_after_base",
        "base": {"kind": "aggregate", "measure": "measure.jaffle.session_starts"},
        "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
    }
    query = {
        "version": 2,
        "select": [{"as": "rate", "expression": conversion}],
        "time": {
            "temporal_role": SESSION_ROLE,
            "grain": "day",
            "start": "2024-06-30",
            "end": "2024-07-01T23:00:00",
        },
    }
    return config, query


@pytest.mark.parametrize("data_type", ["date", "timestamp"])
def test_conversion_metric_refuses_a_converted_query_clock(config, data_type):
    """Its leaf would bucket and bound the stored session days, not the local ones."""
    config, query = _session_conversion(config, data_type, column_timezone="UTC")
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), query)
    assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert caught.value.details == {
        "temporal_role": SESSION_ROLE,
        "column_timezone": "UTC",
        "timezone": "America/New_York",
        "path": "conversion_metric",
    }


def test_conversion_metric_on_an_unconverted_clock_matches_its_reference(config):
    config, query = _session_conversion(config, "date", column_timezone="")
    sql = compile_query(config, Registry(config), query)["sql"]
    with duckdb.connect(":memory:") as connection:
        connection.execute(SESSION_SEED)
        actual = connection.execute(sql).fetchall()
        reference = connection.execute(SESSION_REFERENCE).fetchall()
    assert actual == reference == [(datetime(2024, 6, 30), 0.0), (datetime(2024, 7, 1), 1.0)]


def _query(**time):
    return {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "n"}],
        "time": {
            "temporal_role": ROLE,
            "grain": "month",
            "range": {"last": {"unit": "month", "value": 1}},
            **time,
        },
    }


@pytest.mark.parametrize(
    "now",
    [
        "2024-07-01T02:00:00Z",
        "2024-06-30T22:00:00-04:00",
        datetime(2024, 7, 1, 2, tzinfo=UTC),
        "2024-06-30",
        date(2024, 6, 30),
        datetime(2024, 6, 30, 22),
    ],
)
@pytest.mark.parametrize("normalize", [ast.normalize_query, ast.normalize_partial_query])
def test_range_normalization_preserves_the_instant_until_the_role_is_known(config, now, normalize):
    query = {**_query(), "policy_context": {"now": now}}
    time = normalize(query, config=config).time
    assert (time.start, time.end) == ("2024-05-01", "2024-06-01")


@pytest.mark.parametrize(
    "timezone,end",
    [("UTC", "2024-07-01"), ("America/New_York", "2024-06-01"), ("Asia/Tokyo", "2024-07-01")],
)
def test_default_now_uses_the_roles_date(monkeypatch, config, timezone, end):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 7, 1, 2, tzinfo=UTC)

    monkeypatch.setattr(ast, "datetime", FixedDatetime)
    config = replace(
        config, temporal_roles=[replace(r, timezone=timezone) for r in config.temporal_roles]
    )
    assert ast.normalize_query(_query(), config=config).time.end == end


def test_utc_default_keeps_planner_resolution_consistent():
    last = {"last": {"unit": "month", "value": 1}}
    for instant in ("2024-07-01T02:00:00Z", "2024-06-30T22:00:00-04:00"):
        assert ast._relative_range_bounds(last, policy_context={"now": instant}) == {
            "start": "2024-06-01",
            "end": "2024-07-01",
        }


@pytest.mark.parametrize("read", [_query_state, values_query_state])
def test_query_state_reports_the_same_local_bounds(config, read):
    state = read({**_query(), "policy_context": {"now": "2024-07-01T02:00:00Z"}}, config)
    assert state["normalized_query"]["time"]["end"] == "2024-06-01"


def test_cache_changes_at_local_midnight_within_one_utc_day(monkeypatch, config):
    class MovingDatetime(datetime):
        hour = 3

        @classmethod
        def now(cls, tz=None):
            return cls(2024, 7, 1, cls.hour, tzinfo=UTC)

    monkeypatch.setattr(ast, "datetime", MovingDatetime)
    runtime = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        first = runtime._compile(_query(), policy_context={})
        MovingDatetime.hour = 4
        second = runtime._compile(_query(), policy_context={})
        again = runtime._compile(_query(), policy_context={})
        assert first["logical_plan"].time["end"] == "2024-06-01"
        assert second["logical_plan"].time["end"] == "2024-07-01"
        assert second["compile_stats"]["cache_hit"] is False
        assert again["compile_stats"]["cache_hit"] is True
    finally:
        runtime.close()


@pytest.mark.parametrize("unit", ["week", "month", "quarter", "year"])
def test_nested_compilation_refuses_nondefault_relative_periods(config, unit):
    query = _query(calendar_id="fiscal", range={"last": {"unit": unit, "value": 9}})
    with pytest.raises(SemanticLayerError) as caught:
        _compile_query_sql_ast(config, query)
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.details["path"] == "time.range.last.unit"


def test_cache_reuses_the_date_already_resolved_by_binding(monkeypatch, config):
    class MovingDatetime(datetime):
        hour = 3

        @classmethod
        def now(cls, tz=None):
            return cls(2024, 7, 1, cls.hour, tzinfo=UTC)

    monkeypatch.setattr(ast, "datetime", MovingDatetime)
    runtime = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        binding = runtime._bind(_query(), {})
        MovingDatetime.hour = 4
        first = runtime._compile(_query(), policy_context={}, binding=binding)
        second = runtime._compile(_query(), policy_context={}, binding=runtime._bind(_query(), {}))
        assert first["logical_plan"].time["end"] == "2024-06-01"
        assert second["logical_plan"].time["end"] == "2024-07-01"
        assert second["compile_stats"]["cache_hit"] is False
    finally:
        runtime.close()


def test_day_ranges_remain_available_on_nondefault_calendars(config):
    query = {
        **_query(calendar_id="fiscal", range={"last": {"unit": "day", "value": 1}}),
        "policy_context": {"now": "2024-07-01T02:00:00Z"},
    }
    time = ast.normalize_query(query, config=config).time
    assert (time.start, time.end) == ("2024-06-29", "2024-06-30")


def test_role_bound_to_nondefault_calendar_cannot_bypass_the_period_guard(config):
    role = next(r for r in config.temporal_roles if r.id == ROLE)
    entity = next(d.entity for d in config.dimensions if d.id == role.dimension)
    config = replace(
        config,
        entities=[
            replace(e, calendar_id="fiscal") if e.id == entity else e for e in config.entities
        ],
    )
    with pytest.raises(SemanticLayerError) as caught:
        ast.normalize_query(_query(), config=config)
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.details["calendar_id"] == "fiscal"
