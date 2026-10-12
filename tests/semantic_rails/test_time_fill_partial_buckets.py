"""A filled time series keeps the partial buckets at the edges of the window.

With ``time.fill: true`` the series of buckets came from the calendar's
bucket-start column, filtered to the window. A week or month that starts
before ``start`` was left out, so its rows from inside the window vanished:
July 2017 by week lost the week of June 26, which holds July 1-2. Every fill
now uses the implicit Gregorian calendar; an authored one takes no part.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime

import duckdb
import pytest

from semantic_rails.clock import period_end
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config


def test_parallel_window_finalization_with_empty_input():
    # Empty-group settlement adds a window over a stream with empty input blocks.
    # DuckDB before 1.5.6 could finalize those blocks before their sink tasks ran.
    # This reduced query reproduces the race without dates or shared connections.
    sql = """
        WITH t AS (SELECT 0 AS x), u AS (
            SELECT * FROM t WHERE x >= 5000
            UNION ALL
            SELECT * FROM t
        )
        SELECT x, COUNT(*) OVER () FROM u ORDER BY x DESC
    """
    with duckdb.connect(config={"threads": 4}) as connection:
        for _ in range(1000):
            assert connection.execute(sql).fetchall() == [(0, 1)]


@pytest.fixture(scope="module")
def shared_package(tmp_path_factory):
    """The packaged jaffle shop for tests that only read it."""
    return copy_package_config(tmp_path_factory.mktemp("jaffle"), "jaffle_shop", preseed_db=True)


def _as_date(value) -> date:
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    return value.date() if isinstance(value, datetime) else value


# A call's named window (``start`` and ``end``) answers every bucket it touches; the open
# window from the same start (no ``end``) keeps only the buckets with rows.
ORDERS = {
    "expression": {"kind": "measure", "measure": "measure.jaffle.order_count"},
    "as": "orders",
}


def _series(runtime, grain: str, start: str, end: str | None, **time) -> dict[date, int]:
    rows = runtime.query_internal(
        {
            "select": [ORDERS],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": grain,
                "start": start,
                **({"end": end} if end else {}),
                **time,
            },
        }
    )["rows"]
    return {_as_date(row["period"]): row["orders"] for row in rows}


@pytest.mark.parametrize(
    ("grain", "start", "end", "first_bucket"),
    [
        ("week", "2017-07-01", "2017-08-01", date(2017, 6, 26)),
        ("month", "2017-07-15", "2017-10-01", date(2017, 7, 1)),
    ],
)
def test_fill_keeps_the_bucket_that_starts_before_the_window(
    runtime_factory, grain, start, end, first_bucket
):
    runtime = runtime_factory("jaffle_shop")
    try:
        filled = _series(runtime, grain, start, end)
        open_ended = _series(runtime, grain, start, None)
    finally:
        runtime.close()

    assert min(filled) == first_bucket

    # Filling only adds empty buckets: every bucket with rows that ends inside the window
    # keeps the count the open window reads (the last bucket may be cut by the end).
    def inside(series):
        last = date.fromisoformat(end)
        return {b: n for b, n in series.items() if n and period_end(b, grain) <= last}

    assert inside(filled) == inside(open_ended)


_JULY_BY_WEEK = {
    "version": 1,
    "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
    "time": {
        "temporal_role": "temporal_role.jaffle_order_time",
        "grain": "week",
        "start": "2017-07-01",
        "end": "2017-08-01",
        "fill": True,
    },
}


def _adjusted(config, *, zone=None, timestamp_day=False):
    """The package with the order clock in ``zone``, or its calendar day typed a timestamp."""
    if timestamp_day:
        dimensions = [
            dataclasses.replace(row, data_type="timestamp")
            if row.entity == "entity.jaffle_time" and row.column == "date_day"
            else row
            for row in config.dimensions
        ]
        config = dataclasses.replace(config, dimensions=dimensions)
    if zone:
        roles = [
            dataclasses.replace(row, timezone=zone)
            if row.id == "temporal_role.jaffle_order_time"
            else row
            for row in config.temporal_roles
        ]
        config = dataclasses.replace(config, temporal_roles=roles)
    return config


def _with_calendar(config, *, key=None, keep_date_day=True):
    """The package with its calendar re-keyed or without its date_day dimension."""
    calendar = next(row for row in config.entities if row.id == "entity.jaffle_time")
    entities = [
        dataclasses.replace(row, key=key) if row is calendar and key else row
        for row in config.entities
    ]
    dimensions = [
        row
        for row in config.dimensions
        if keep_date_day or not (row.entity == calendar.id and row.column == "date_day")
    ]
    return dataclasses.replace(config, entities=entities, dimensions=dimensions)


@pytest.mark.parametrize("key", [["date_id"], ["date_key"]], ids=["integer-id", "text-key"])
def test_fill_bounds_the_window_by_date_day_not_the_calendar_key(runtime_factory, key):
    # A calendar can be keyed by a surrogate. The window must still be bounded by its
    # date_day column; the surrogate column doesn't even exist here.
    runtime = runtime_factory("jaffle_shop")
    try:
        config = _with_calendar(runtime.config, key=key)
        sql = compile_query(config, Registry(config), _JULY_BY_WEEK)["sql"]
        rows = runtime._get_adapter().query(sql)
    finally:
        runtime.close()

    assert key[0] not in sql
    alias = "period"
    assert min(_as_date(row[alias]) for row in rows) == date(2017, 6, 26)
    assert sum(row["orders"] or 0 for row in rows) == 7438


def test_fill_without_a_date_day_column_uses_the_implicit_calendar(runtime_factory):
    # The authored calendar takes no part, so its missing date_day changes nothing.
    runtime = runtime_factory("jaffle_shop")
    try:
        config = _with_calendar(runtime.config, keep_date_day=False)
        sql = compile_query(config, Registry(config), _JULY_BY_WEEK)["sql"]
    finally:
        runtime.close()

    assert "implicit_calendar" in sql and "jaffle_calendar" not in sql


def _bucket_counts(runtime, grain: str, start: str, end: str, *, fill: bool, group_by=None):
    query = {
        "select": [ORDERS],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": grain,
            "start": start,
            "end": end,
            "fill": fill,
        },
    }
    if group_by:
        query["group_by"] = [group_by]
    rows = runtime.query_internal(query)["rows"]
    return {
        (_as_date(row["period"]), row[group_by] if group_by else None): row["orders"]
        for row in rows
        if row["orders"]
    }


@pytest.mark.parametrize(
    ("grain", "start", "end", "group_by"),
    [
        ("month", "2017-07-01 12:00:00", "2017-08-01 12:00:00", None),
        ("week", "2017-07-02 08:00:00", "2017-07-02 18:00:00", None),
        ("week", "2017-07-01", "2017-08-01 00:00:00", None),
        ("quarter", "2017-07-15", "2017-10-01", None),
        ("year", "2017-07-01", "2017-08-01", None),
        ("week", "2017-07-01", "2017-08-01", "dimension.jaffle_store_name"),
    ],
    ids=["time-of-day-edges", "same-day", "midnight-end", "quarter", "year", "group-by"],
)
def test_fill_keeps_every_bucket_with_rows(runtime_factory, grain, start, end, group_by):
    # Whatever the window, filling only adds empty buckets.
    runtime = runtime_factory("jaffle_shop")
    try:
        filled = _bucket_counts(runtime, grain, start, end, fill=True, group_by=group_by)
        unfilled = _bucket_counts(runtime, grain, start, end, fill=False, group_by=group_by)
    finally:
        runtime.close()

    assert unfilled
    assert filled == unfilled


def test_timestamp_calendar_keeps_base_intraday_bounds(tmp_path):
    # Timestamp calendar storage is ambiguous; the implicit calendar never reads it.
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "ALTER TABLE jaffle_calendar ALTER COLUMN date_day SET DATA TYPE TIMESTAMP"
        )
    runtime = Runtime.from_path(str(package_dir))
    try:
        config = _adjusted(runtime.config, timestamp_day=True)
        query = {
            **_JULY_BY_WEEK,
            "time": {**_JULY_BY_WEEK["time"], "start": "2017-07-03T12:00:00", "end": "2017-07-04"},
        }
        sql = compile_query(config, Registry(config), query)["sql"]
        rows = runtime._get_adapter().query(sql)
        unfilled = _bucket_counts(runtime, "week", "2017-07-03T12:00:00", "2017-07-04", fill=False)
    finally:
        runtime.close()

    alias = "period"
    assert unfilled
    assert {
        (_as_date(row[alias]), None): row["orders"] for row in rows if row["orders"]
    } == unfilled
    assert "jaffle_calendar" not in sql


def test_timestamp_calendar_retains_base_partial_week_limit(tmp_path):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "ALTER TABLE jaffle_calendar ALTER COLUMN date_day SET DATA TYPE TIMESTAMP"
        )
        config = load_package_config(str(package_dir))
        config = _adjusted(config, timestamp_day=True)
        sql = compile_query(config, Registry(config), _JULY_BY_WEEK)["sql"]
        rows = connection.execute(
            f"SELECT CAST(period AS DATE), orders FROM ({sql}) AS timestamp_result"
        ).fetchall()

    assert rows
    # The week of June 26 holds July 1-2 (see test_fill_keeps_the_bucket_that_starts_...).
    assert min(day for day, _ in rows) == date(2017, 6, 26)
    assert "jaffle_calendar" not in sql


def test_timestamp_calendar_keeps_raw_bounds_across_session_timezone(tmp_path):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute("SET TimeZone='UTC'")
        connection.execute(
            "ALTER TABLE jaffle_calendar ALTER COLUMN date_day SET DATA TYPE TIMESTAMPTZ"
        )
        connection.execute("UPDATE jaffle_calendar SET date_day = date_day - INTERVAL '9 hours'")
        connection.execute("DELETE FROM jaffle_order")
        config = load_package_config(str(package_dir))
        config = _adjusted(config, zone="Asia/Tokyo", timestamp_day=True)
        query = {
            **_JULY_BY_WEEK,
            "time": {
                **_JULY_BY_WEEK["time"],
                "grain": "day",
                "start": "2017-07-04T00:00:00+09:00",
                "end": "2017-07-05T00:00:00+09:00",
            },
        }
        sql = compile_query(config, Registry(config), query)["sql"]
        rows = connection.execute(
            f"SELECT CAST(period AS VARCHAR), orders FROM ({sql}) AS tokyo_result"
        ).fetchall()

    # No order exists at all, so there is no data to call zero: Tokyo's July 4 reads NULL.
    assert rows == [("2017-07-04 00:00:00", None)]
    assert "jaffle_calendar" not in sql


@pytest.mark.parametrize(
    ("zone", "start", "end", "first", "second", "days"),
    [
        (
            "UTC",
            "2017-07-03T00:00:00Z",
            "2017-07-04T00:00:00-02:00",
            "2017-07-03 15:00:00+00",
            "2017-07-04 01:00:00+00",
            (date(2017, 7, 3), date(2017, 7, 4)),
        ),
        (
            "America/New_York",
            "2017-07-04T00:00:00-04:00",
            "2017-07-04T23:00:00-07:00",
            "2017-07-04 15:00:00+00",
            "2017-07-05 05:00:00+00",
            (date(2017, 7, 4), date(2017, 7, 5)),
        ),
    ],
    ids=["utc-calendar", "new-york-calendar"],
)
def test_fill_preserves_offset_window_days_with_timestamptz_data(
    tmp_path, zone, start, end, first, second, days
):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute(f"SET TimeZone='{zone}'")
        connection.execute(
            "ALTER TABLE jaffle_calendar ALTER COLUMN date_day SET DATA TYPE TIMESTAMPTZ"
        )
        connection.execute(
            "ALTER TABLE jaffle_order ALTER COLUMN ordered_at SET DATA TYPE TIMESTAMPTZ"
        )
        connection.execute("DELETE FROM jaffle_order")
        connection.execute(
            "INSERT INTO jaffle_order (order_id, ordered_at) VALUES "
            f"('first', TIMESTAMPTZ '{first}'), "
            f"('second', TIMESTAMPTZ '{second}')"
        )

        config = load_package_config(str(package_dir))
        config = _adjusted(config, zone=zone, timestamp_day=True)
        query = {
            **_JULY_BY_WEEK,
            "time": {
                **_JULY_BY_WEEK["time"],
                "grain": "day",
                "start": start,
                "end": end,
            },
        }

        def rows(fill):
            shape = {**query, "time": {**query["time"], "fill": fill}}
            sql = compile_query(config, Registry(config), shape)["sql"]
            return connection.execute(
                f"SELECT CAST(period AS DATE), orders FROM ({sql}) AS filled_result"
            ).fetchall()

        filled = rows(True)
        unfilled = rows(False)

    expected = {day: 1 for day in days}
    assert len(unfilled) == len(filled) == len(expected)
    assert dict(unfilled) == expected
    assert dict(filled) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2017-11-05T01:30:00-04:00",
            "2017-11-05T01:30:00-05:00",
            {date(2017, 11, 5): 1},
        ),
        ("2017-11-05T01:30:00-05:00", "2017-11-05T01:45:00-04:00", {}),
    ],
    ids=["positive-repeated-hour", "reversed-repeated-hour"],
)
def test_fill_respects_instant_order_during_dst_fold(tmp_path, start, end, expected):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute("SET TimeZone='America/New_York'")
        connection.execute(
            "ALTER TABLE jaffle_order ALTER COLUMN ordered_at SET DATA TYPE TIMESTAMPTZ"
        )
        connection.execute("DELETE FROM jaffle_order")
        connection.execute(
            "INSERT INTO jaffle_order (order_id, ordered_at) "
            "VALUES ('fold', TIMESTAMPTZ '2017-11-05 01:45:00-04:00')"
        )
        config = load_package_config(str(package_dir))
        config = _adjusted(config, zone="America/New_York")
        time = {**_JULY_BY_WEEK["time"], "grain": "day", "start": start, "end": end}
        results = {}
        for fill in (False, True):
            shape = {**_JULY_BY_WEEK, "time": {**time, "fill": fill}}
            sql = compile_query(config, Registry(config), shape)["sql"]
            rows = connection.execute(
                f"SELECT CAST(period AS DATE), orders FROM ({sql}) AS fold_result"
            ).fetchall()
            results[fill] = dict(rows)

    assert results[False] == expected
    assert results[True] == expected


def test_fill_keeps_source_bucket_with_offset_bounds_on_plain_timestamp(tmp_path):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute("DELETE FROM jaffle_order")
        connection.execute(
            "INSERT INTO jaffle_order (order_id, ordered_at) "
            "VALUES ('wall', TIMESTAMP '2017-07-03 23:30:00')"
        )
        config = load_package_config(str(package_dir))
        time = {
            **_JULY_BY_WEEK["time"],
            "grain": "day",
            "start": "2017-07-03T23:00:00-02:00",
            "end": "2017-07-04T01:00:00-02:00",
        }
        results = {}
        for fill in (False, True):
            shape = {**_JULY_BY_WEEK, "time": {**time, "fill": fill}}
            sql = compile_query(config, Registry(config), shape)["sql"]
            rows = connection.execute(
                f"SELECT CAST(period AS DATE), orders FROM ({sql}) AS wall_result"
            ).fetchall()
            results[fill] = {day: count for day, count in rows if count}

    assert results[False] == {date(2017, 7, 3): 1}
    assert results[True] == results[False]


@pytest.mark.parametrize(
    ("start", "end", "expected", "empty_interval"),
    [
        ("2017-07-03T00:00:00", "2017-07-04T00:00:00.000000002", {date(2017, 7, 4): 1}, False),
        ("2017-07-04T00:00:00", "2017-07-04T00:00:00.000000002", {date(2017, 7, 4): 1}, False),
        (
            "2017-07-04T00:00:00.000000001",
            "2017-07-04T00:00:00.000000002",
            {date(2017, 7, 4): 1},
            False,
        ),
        ("2017-07-03T00:00:00", "2017-07-04T00:00:00", {}, False),
        ("2017-07-04T00:00:00.000000001", "2017-07-04T00:00:00.000000001", {}, True),
        ("2017-07-04T00:00:00.000000002", "2017-07-04T00:00:00.000000001", {}, True),
        (
            "2017-07-04T00:00:00+00:00",
            "2017-07-04T00:00:00.000000002+00:00",
            {date(2017, 7, 4): 1},
            False,
        ),
    ],
    ids=[
        "end-after-midnight",
        "positive-submicrosecond",
        "positive-within-one-microsecond",
        "exact-midnight-end",
        "equal-submicrosecond",
        "reversed-submicrosecond",
        "offset-positive-submicrosecond",
    ],
)
def test_fill_preserves_nanosecond_window_bounds(tmp_path, start, end, expected, empty_interval):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "ALTER TABLE jaffle_order ALTER COLUMN ordered_at SET DATA TYPE TIMESTAMP_NS"
        )
        connection.execute("DELETE FROM jaffle_order")
        connection.execute(
            "INSERT INTO jaffle_order (order_id, ordered_at) "
            "VALUES ('one-nanosecond', TIMESTAMP_NS '2017-07-04 00:00:00.000000001')"
        )
        config = load_package_config(str(package_dir))
        time = {**_JULY_BY_WEEK["time"], "grain": "day", "start": start, "end": end}
        results = {}
        for fill in (False, True):
            query = {**_JULY_BY_WEEK, "time": {**time, "fill": fill}}
            sql = compile_query(config, Registry(config), query)["sql"]
            rows = connection.execute(
                f"SELECT CAST(period AS DATE), orders FROM ({sql}) AS nanosecond_result"
            ).fetchall()
            results[fill] = rows

    assert {day: count for day, count in results[False] if count} == expected
    assert {day: count for day, count in results[True] if count} == expected
    if empty_interval:
        assert results[True] == []


LAST_DAYS = [date(9999, 12, 30), date(9999, 12, 31)]


@pytest.mark.parametrize(
    ("start", "end", "days"),
    [
        ("9999-12-30", "9999-12-31T23:59:59", LAST_DAYS),
        ("9999-12-30", "9999-12-31T00:00:00", LAST_DAYS[:1]),
        ("9999-12-30", "9999-12-31T00:00:00.000000001", LAST_DAYS),
        ("0001-01-01T00:00:00.000000001", "0001-01-03", [date(1, 1, 1), date(1, 1, 2)]),
        ("9999-12-31T00:00:00.000000001", "9999-12-31T00:00:00.000000001", []),
        ("9999-12-31T00:00:00.000000002", "9999-12-31T00:00:00.000000001", []),
    ],
    ids=[
        "max-late",
        "max-midnight",
        "max-submicrosecond",
        "min-submicrosecond",
        "max-equal",
        "max-reversed",
    ],
)
def test_fill_boundary_dates_do_not_overflow(runtime_factory, start, end, days):
    runtime = runtime_factory("jaffle_shop")
    query = {
        **_JULY_BY_WEEK,
        "time": {**_JULY_BY_WEEK["time"], "grain": "day", "start": start, "end": end},
    }
    try:
        sql = compile_query(runtime.config, Registry(runtime.config), query)["sql"]
        rows = runtime._get_adapter().query(sql)
    finally:
        runtime.close()

    # Every day the window touches, outside the loaded range, so with no data.
    assert [_as_date(row["period"]) for row in rows] == days
    assert all(row["orders"] is None for row in rows)


@pytest.mark.parametrize(
    ("zone", "start", "end", "expected"),
    [
        pytest.param("UTC", "9999-12-30T00:00:00", "9999-12-31T00:00:00", [date(9999, 12, 30)], id="maximum-exact-midnight-excludes-last-day"),
        pytest.param("UTC", "9999-12-30T00:00:00", "9999-12-31T00:00:00.000000001", [date(9999, 12, 30), date(9999, 12, 31)], id="maximum-submicrosecond-includes-last-day"),
        pytest.param("America/New_York", "9999-12-30T00:00:00+00:00", "9999-12-31T23:59:59+00:00", [date(9999, 12, 29), date(9999, 12, 30), date(9999, 12, 31)], id="maximum-in-negative-offset-zone"),
        pytest.param("Asia/Tokyo", "0001-01-01T00:00:00+00:00", "0001-01-02T00:00:00+00:00", [date(1, 1, 1), date(1, 1, 2)], id="minimum-in-positive-offset-zone"),
        pytest.param("UTC", "0001-01-01T00:00:00+01:00", "0001-01-02T00:00:00+01:00", [date(1, 1, 1)], id="minimum-offset-crosses-utc-start"),
        pytest.param("UTC", "9999-12-30T00:00:00-01:00", "9999-12-31T23:00:00-01:00", [date(9999, 12, 30), date(9999, 12, 31)], id="maximum-offset-crosses-utc-end"),
        pytest.param("Asia/Tokyo", "0001-01-01T00:30:00+01:00", "0001-01-01T01:30:00+01:00", [date(1, 1, 1)], id="minimum-intermediate-overflow-but-role-day-is-valid"),
        pytest.param("America/New_York", "9999-12-31T22:30:00-01:00", "9999-12-31T23:30:00-01:00", [date(9999, 12, 31)], id="maximum-intermediate-overflow-but-role-day-is-valid"),
        pytest.param("UTC", "0001-01-01T00:00:00+14:00", "0001-01-01T01:00:00+14:00", [], id="minimum-extreme-offset-entirely-before-date-range"),
        pytest.param("UTC", "9999-12-31T23:00:00-14:00", "9999-12-31T23:30:00-14:00", [], id="maximum-extreme-offset-entirely-after-date-range"),
        pytest.param("America/New_York", "0001-01-01T00:00:00+00:00", "0001-01-01T01:00:00+00:00", [], id="minimum-role-conversion-before-date-range"),
        pytest.param("Asia/Tokyo", "9999-12-31T23:00:00+00:00", "9999-12-31T23:30:00+00:00", [], id="maximum-role-conversion-after-date-range"),
        pytest.param("UTC", "0001-01-01T00:00:00+01:00", "0001-01-01T00:00:00+01:00", [], id="minimum-offset-equal-interval"),
        pytest.param("UTC", "9999-12-31T23:30:00-01:00", "9999-12-31T23:00:00-01:00", [], id="maximum-offset-reversed-interval"),
        pytest.param("UTC", "0001-01-01T00:00:00+01:00", "0001-01-01T00:00:00+00:00", [], id="minimum-exact-midnight-end"),
        pytest.param("UTC", "0001-01-01T00:00:00+01:00", "0001-01-01T00:00:00.000000001+00:00", [date(1, 1, 1)], id="minimum-submicrosecond-end"),
        pytest.param("UTC", "9999-12-31T23:59:59.999999998+00:00", "9999-12-31T23:59:59.999999999+00:00", [date(9999, 12, 31)], id="maximum-last-nanosecond-window"),
        pytest.param("UTC", "0001-01-01T01:00:00+01:00", "0001-01-01T00:00:00+00:00", [], id="minimum-equivalent-instants-different-offsets"),
        pytest.param("UTC", "9999-12-31T23:00:00.000000002-01:00", "9999-12-31T23:00:00.000000001-01:00", [], id="maximum-reversed-submicrosecond-offset-window"),
        pytest.param("UTC", "0001-01-01T01:00:00,123456+01:00", "0001-01-01T00:00:00.123456+00:00", [], id="minimum-equivalent-instants-comma-fraction"),
        pytest.param("Asia/Tokyo", "0001-01-01T00:41:30+10:00", "0001-01-01T00:42:30+10:00", [date(1, 1, 1)], id="minimum-historic-second-offset-selects-day"),
        pytest.param("Asia/Tokyo", "0001-01-01T00:40:00+10:00", "0001-01-01T00:41:01+10:00", [], id="minimum-historic-second-offset-midnight-end"),
        pytest.param("Asia/Tokyo", "0001-01-01T00:40:00+10:00", "0001-01-01T00:41:01.000000001+10:00", [date(1, 1, 1)], id="minimum-historic-second-offset-nanosecond-end"),
    ],
)  # fmt: skip
def test_fill_date_extremes_with_representable_zone_conversion(
    shared_package, zone, start, end, expected
):
    package_dir = shared_package
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb"), read_only=True) as connection:
        config = load_package_config(str(package_dir))
        config = _adjusted(config, zone=zone)
        query = {
            **_JULY_BY_WEEK,
            "time": {**_JULY_BY_WEEK["time"], "grain": "day", "start": start, "end": end},
        }
        sql = compile_query(config, Registry(config), query)["sql"]
        rows = connection.execute(
            f"SELECT CAST(period AS DATE), orders FROM ({sql}) AS boundary_result"
        ).fetchall()

    assert [day for day, _ in rows] == expected
    # These buckets are outside the loaded date range, including placeholder dates.
    assert all(count is None for _, count in rows)


def test_offset_fill_refuses_on_clickhouse():
    # ClickHouse has no implicit calendar, and an authored one takes no part in a fill.
    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    config = dataclasses.replace(
        config, package=dataclasses.replace(config.package, warehouse="clickhouse")
    )
    query = {
        **_JULY_BY_WEEK,
        "time": {
            **_JULY_BY_WEEK["time"],
            "grain": "day",
            "start": "2017-07-03T23:00:00-02:00",
            "end": "2017-07-04T01:00:00-02:00",
        },
    }
    with pytest.raises(SemanticLayerError, match="no implicit calendar"):
        compile_query(config, Registry(config), query)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2017-07-03T12:00:00", "2017-07-03T12:00:00"),
        ("2017-07-03T18:00:00", "2017-07-03T08:00:00"),
    ],
    ids=["equal", "reversed"],
)
def test_fill_has_no_rows_for_an_empty_interval(runtime_factory, start, end):
    runtime = runtime_factory("jaffle_shop")
    query = {
        **_JULY_BY_WEEK,
        "time": {**_JULY_BY_WEEK["time"], "grain": "day", "start": start, "end": end},
    }
    try:
        filled = runtime.query_internal(query)["rows"]
        unfilled = runtime.query_internal({**query, "time": {**query["time"], "fill": False}})[
            "rows"
        ]
    finally:
        runtime.close()
    assert filled == unfilled == []


@pytest.mark.parametrize(
    ("bounds", "day_type"),
    [
        ({}, "date"),
        ({"start": "2017-07-01"}, "timestamp"),
        ({"end": "2017-08-01"}, "date"),
        ({"start": "2017-07-01", "end": "2017-08-01"}, "timestamp"),
    ],
    ids=["unbounded", "start-only", "end-only", "both-bounds"],
)
def test_fill_never_binds_or_checks_the_calendar_day(bounds, day_type):
    from semantic_rails.compiler import bind_query
    from semantic_rails.policies import enforce_query_policies
    from semantic_rails.schema import SemanticPolicyConfig

    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    config = _adjusted(config, timestamp_day=day_type == "timestamp")
    day_id = next(
        row.id
        for row in config.dimensions
        if row.entity == "entity.jaffle_time" and row.column == "date_day"
    )
    time = {
        key: value for key, value in _JULY_BY_WEEK["time"].items() if key not in {"start", "end"}
    }
    query = {**_JULY_BY_WEEK, "time": {**time, **bounds}}
    bound = bind_query(config, None, query)
    assert day_id not in bound.object_ids

    denied_day = dataclasses.replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test.calendar_day",
                kind="object_access",
                object_ids=[day_id],
                action="deny",
            )
        ],
    )
    assert enforce_query_policies(denied_day, bound.object_ids, query=query) == []
