"""A filled time series keeps the partial buckets at the edges of the window.

With ``time.fill: true`` the series of buckets came from the calendar's
bucket-start column, filtered to the window. A week or month that starts
before ``start`` was left out, so its rows from inside the window vanished:
July 2017 by week lost the week of June 26, which holds July 1-2.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _series(runtime, grain: str, start: str, end: str, *, fill: bool, **time) -> dict[date, int]:
    alias = f"temporal_role.jaffle_order_time__{grain}"
    rows = runtime.query(
        {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": grain,
                "start": start,
                "end": end,
                "fill": fill,
                **time,
            },
        }
    )["rows"]
    return {_as_date(row[alias]): row["orders"] for row in rows}


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
        filled = _series(runtime, grain, start, end, fill=True)
        unfilled = _series(runtime, grain, start, end, fill=False)
    finally:
        runtime.close()

    assert min(filled) == first_bucket
    # Filling only adds empty buckets: every bucket with rows keeps its count.
    assert {bucket: orders for bucket, orders in filled.items() if orders} == unfilled


def test_fiscal_fill_keeps_the_fiscal_quarter_that_starts_before_the_window(runtime_factory):
    # A fiscal query needs fill, so compare it with orders grouped by the fiscal calendar
    # over the same half-open window. The fiscal quarter of November 2016 holds January.
    runtime = runtime_factory("jaffle_shop")
    try:
        filled = _series(
            runtime, "quarter", "2017-01-01", "2017-10-01", fill=True, calendar_id="fiscal"
        )
        oracle = runtime._get_adapter().query(
            """
            SELECT fiscal.quarter_start AS quarter, COUNT(DISTINCT o.order_id) AS orders
            FROM jaffle_order AS o
            JOIN jaffle_calendar_fiscal AS fiscal
              ON fiscal.date_day = CAST(o.ordered_at AS DATE)
            WHERE o.ordered_at >= '2017-01-01' AND o.ordered_at < '2017-10-01'
            GROUP BY fiscal.quarter_start
            """
        )
    finally:
        runtime.close()

    assert min(filled) == date(2016, 11, 1)
    expected = {_as_date(row["quarter"]): row["orders"] for row in oracle}
    assert {quarter: orders for quarter, orders in filled.items() if orders} == expected


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
    alias = "temporal_role.jaffle_order_time__week"
    assert min(_as_date(row[alias]) for row in rows) == date(2017, 6, 26)
    assert sum(row["orders"] for row in rows) == 7438


def test_fill_without_a_date_day_column_bounds_the_window_by_bucket(runtime_factory):
    # Without a date_day dimension there is no day to bound by, so the series keeps
    # the earlier behavior and filters the bucket column.
    runtime = runtime_factory("jaffle_shop")
    try:
        config = _with_calendar(runtime.config, keep_date_day=False)
        sql = compile_query(config, Registry(config), _JULY_BY_WEEK)["sql"]
    finally:
        runtime.close()

    assert "jaffle_calendar.week_start >= '2017-07-01'" in sql


def _bucket_counts(runtime, grain: str, start: str, end: str, *, fill: bool, group_by=None):
    alias = f"temporal_role.jaffle_order_time__{grain}"
    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
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
    rows = runtime.query(query)["rows"]
    return {
        (_as_date(row[alias]), row[group_by] if group_by else None): row["orders"]
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


def test_fill_with_a_timestamp_date_day_keeps_an_intraday_start(tmp_path):
    # A calendar whose date_day is a midnight timestamp: an intraday start must still
    # keep its own day.
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    with duckdb.connect(str(package_dir / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "ALTER TABLE jaffle_calendar ALTER COLUMN date_day SET DATA TYPE TIMESTAMP"
        )
    runtime = Runtime.from_path(str(package_dir))
    try:
        config = dataclasses.replace(
            runtime.config,
            dimensions=[
                dataclasses.replace(row, data_type="timestamp")
                if row.entity == "entity.jaffle_time" and row.column == "date_day"
                else row
                for row in runtime.config.dimensions
            ],
        )
        query = {
            **_JULY_BY_WEEK,
            "time": {**_JULY_BY_WEEK["time"], "start": "2017-07-03T12:00:00", "end": "2017-07-04"},
        }
        rows = runtime._get_adapter().query(compile_query(config, Registry(config), query)["sql"])
        unfilled = _bucket_counts(runtime, "week", "2017-07-03T12:00:00", "2017-07-04", fill=False)
    finally:
        runtime.close()

    alias = "temporal_role.jaffle_order_time__week"
    assert unfilled
    assert {
        (_as_date(row[alias]), None): row["orders"] for row in rows if row["orders"]
    } == unfilled


def test_a_windowed_fill_binds_the_calendar_day_it_reads():
    # The fill window reads the calendar's date_day, so it is a bound object, checked
    # like the grain column. Without fill the query never reads it.
    from semantic_rails import compiler
    from semantic_rails.config import load_package_config, resolve_repo_path

    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    days = {
        row.id
        for row in config.dimensions
        if row.entity == "entity.jaffle_time" and row.column == "date_day"
    }
    unfilled = {**_JULY_BY_WEEK, "time": {**_JULY_BY_WEEK["time"], "fill": False}}

    assert days & compiler.bind_query(config, None, _JULY_BY_WEEK).object_ids
    assert not days & compiler.bind_query(config, None, unfilled).object_ids


@pytest.mark.parametrize(
    ("bounds", "reads_day"),
    [
        ({}, False),
        ({"start": "2017-07-01"}, False),
        ({"end": "2017-08-01"}, False),
        ({"start": "2017-07-01", "end": "2017-08-01"}, True),
    ],
    ids=["unbounded", "start-only", "end-only", "both-bounds"],
)
def test_fill_binds_and_checks_calendar_day_only_when_sql_reads_it(bounds, reads_day):
    from semantic_rails.compiler import bind_query
    from semantic_rails.config import load_package_config, resolve_repo_path
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.policies import enforce_query_policies
    from semantic_rails.schema import SemanticPolicyConfig

    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
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
    assert (day_id in bound.object_ids) is reads_day

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
    if reads_day:
        with pytest.raises(SemanticLayerError) as excinfo:
            enforce_query_policies(denied_day, bound.object_ids, query=query)
        assert excinfo.value.code == "POLICY_DENIED"
    else:
        assert enforce_query_policies(denied_day, bound.object_ids, query=query) == []
