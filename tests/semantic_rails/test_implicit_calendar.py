"""The implicit Gregorian calendar: rolling, prior_period and time.fill with no authored calendar.

Every answer is checked two ways on DuckDB: against an independent key (calendar arithmetic over
a bucket series, not the engine's row-counted windows over a day spine), and against the same
package with an authored calendar, which must give identical rows.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.dialects import DuckDbDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlBinary, SqlLiteral

# Gaps: no orders at all in February 2024; store b has none in December 2023 to February 2024.
SEED = """
CREATE TABLE orders AS SELECT * FROM (VALUES
  (1, 'a', TIMESTAMP '2023-11-03 10:00:00', 10.0),
  (2, 'b', TIMESTAMP '2023-11-20 23:30:00', 5.0),
  (3, 'a', TIMESTAMP '2023-12-01 02:00:00', 7.0),
  (4, 'a', TIMESTAMP '2024-01-15 12:00:00', 20.0),
  (5, 'b', TIMESTAMP '2024-03-31 23:00:00', 8.0),
  (6, 'a', TIMESTAMP '2024-04-01 03:00:00', 4.0),
  (7, 'a', TIMESTAMP '2024-05-06 09:00:00', 6.0),
  (8, 'b', TIMESTAMP '2024-06-30 12:00:00', 3.0)
) AS t(order_id, store_id, ordered_at, amount);
CREATE TABLE dim_date AS SELECT d::DATE AS date_day,
  date_trunc('week', d)::DATE AS week_start, date_trunc('month', d)::DATE AS month_start,
  date_trunc('quarter', d)::DATE AS quarter_start, date_trunc('year', d)::DATE AS year_start
FROM range(DATE '2023-01-01', DATE '2025-01-01', INTERVAL 1 DAY) AS t(d);
-- A fiscal year that starts in February: its quarters and years start a month late.
CREATE TABLE dim_fiscal AS SELECT d::DATE AS date_day,
  date_trunc('week', d)::DATE AS week_start, date_trunc('month', d)::DATE AS month_start,
  (date_trunc('quarter', d - INTERVAL 1 MONTH) + INTERVAL 1 MONTH)::DATE AS quarter_start,
  (date_trunc('year', d - INTERVAL 1 MONTH) + INTERVAL 1 MONTH)::DATE AS year_start
FROM range(DATE '2023-01-01', DATE '2025-01-01', INTERVAL 1 DAY) AS t(d);
"""

ROLE = "temporal_role.cal_order_ordered_at"
REVENUE: dict[str, Any] = {"measure": "measure.cal.revenue"}
STORE = "dimension.cal_order_store_id"
STEP = {"day": "1 DAY", "week": "7 DAY", "month": "1 MONTH", "quarter": "3 MONTH", "year": "1 YEAR"}


PACKAGE = """
schema_version: 1
package: {id: cal, namespace: cal, warehouse: duckdb, default_db: data/warehouse.duckdb,
  seed: {kind: sql_script, source: data/seed.sql}, schema_strict: true}
"""
ORDERS = """
model:
  id: orders
  label: Orders
  relation: orders
  entities: {order: {}}
  times:
    ordered_at: {label: Order time, column: ordered_at, kind: timestamp, class: event_time,
      supported_grains: [day, week, month, quarter, year], default: true%s}
  dimensions: {store_id: {label: Store, kind: categorical}}
  measures:
    revenue: {label: Revenue, kind: aggregate, expr: amount, accumulation: {kind: flow},
      value_type: currency}
"""
CALENDAR = """
model:
  id: %(id)s
  label: %(id)s
  relation: %(relation)s
  calendar_id: %(calendar)s
  entities: {%(id)s: {}}
  times: {date_day: {label: Day, column: date_day, kind: date, class: calendar_time}}
  dimensions: {week_start: {label: Week, kind: date}, month_start: {label: Month, kind: date},
    quarter_start: {label: Quarter, kind: date}, year_start: {label: Year, kind: date}}
"""


def _write_package(root: Path, *, calendars: tuple[str, ...] = (), zone: bool = False) -> Path:
    """A strict package over ``orders``, with the named calendars ("default", "fiscal")."""
    package = root / "cal"
    zoned = ", timezone: America/New_York, column_timezone: UTC" if zone else ""
    files = {"data/seed.sql": SEED, "package.yml": PACKAGE, "models/orders.yml": ORDERS % zoned}
    entities: dict[str, Any] = {"order": {"label": "Order", "key": ["order_id"], "model": "orders"}}
    for calendar, relation in (("default", "dim_date"), ("fiscal", "dim_fiscal")):
        if calendar in calendars:
            model = f"{calendar}_calendar"
            files[f"models/{model}.yml"] = CALENDAR % {
                "id": model,
                "relation": relation,
                "calendar": calendar,
            }
            entities[model] = {"label": model, "kind": "time", "key": ["date_day"], "model": model}
            entities[model]["allowed_as_root"] = False
    files["graph.yml"] = yaml.safe_dump({"graph": {"entities": entities}})
    for name, text in files.items():
        (package / name).parent.mkdir(parents=True, exist_ok=True)
        (package / name).write_text(text, encoding="utf-8")
    return package


def _query(package: Path, query: dict[str, Any]) -> tuple[list[tuple[Any, ...]], str]:
    """Rows as sorted tuples (dates for buckets, floats for amounts) and the rendered SQL."""
    engine = Runtime.from_path(str(package))
    try:
        result = engine.query({"version": 1, **query})
    finally:
        engine.close()
    return _sorted(_normal(tuple(row.values())) for row in result["rows"]), str(
        result["rendered_sql"]
    )


def _key(package: Path, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        return _sorted(_normal(row) for row in conn.execute(sql).fetchall())


def _sorted(rows: Any) -> list[tuple[Any, ...]]:
    """By the key columns (the group, then the bucket), which are unique per row."""
    return sorted(rows, key=lambda row: [str(value) for value in row[:2]])


def _normal(row: tuple[Any, ...]) -> tuple[Any, ...]:
    def one(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, (Decimal, float)):
            return round(float(value), 6)
        return value

    return tuple(one(value) for value in row)


def _time(grain: str, **extra: Any) -> dict[str, Any]:
    return {"temporal_role": ROLE, "grain": grain, **extra}


def _series_key(grain: str, value_sql: str, *, local: str = "ordered_at") -> str:
    """One row per bucket from the first to the last bucket with data, plus ``value_sql``.

    ``value_sql`` reads ``s.bucket`` (the series) and ``m`` (revenue per bucket with data);
    the series steps by calendar arithmetic, independently of the engine's day spine.
    """
    return f"""
        WITH m AS (
          SELECT date_trunc('{grain}', {local}) AS bucket, SUM(amount) AS revenue
          FROM orders GROUP BY 1
        ),
        s AS (
          SELECT r.bucket FROM range(
            (SELECT MIN(bucket) FROM m),
            (SELECT MAX(bucket) FROM m) + INTERVAL {STEP[grain]},
            INTERVAL {STEP[grain]}
          ) AS r(bucket)
        )
        SELECT s.bucket, {value_sql} FROM s ORDER BY 1
    """


def _revenue_at(offset: str) -> str:
    """Revenue of the bucket ``offset`` before, 0 when it has no orders, NULL outside the series."""
    return (
        f"CASE WHEN s.bucket - INTERVAL {offset} < (SELECT MIN(bucket) FROM s) THEN NULL "
        f"ELSE COALESCE((SELECT m.revenue FROM m WHERE m.bucket = s.bucket - INTERVAL {offset}), 0)"
        " END"
    )


def _trailing(count: int, unit: str) -> str:
    return (
        "(SELECT COALESCE(SUM(m.revenue), 0) FROM m"
        f" WHERE m.bucket <= s.bucket AND m.bucket > s.bucket - INTERVAL {count} {unit})"
    )


REVENUE_NOW = "COALESCE((SELECT m.revenue FROM m WHERE m.bucket = s.bucket), 0)"
NOW = {"expression": REVENUE, "as": "revenue"}


def _prior(unit: str) -> dict[str, Any]:
    offset = {"unit": unit, "value": 1}
    return {
        "expression": {"kind": "prior_period", "input": REVENUE, "offset": offset},
        "as": "prior",
    }


def _rolling(unit: str, value: int) -> dict[str, Any]:
    window = {"unit": unit, "value": value}
    return {"expression": {"kind": "rolling", "input": REVENUE, "window": window}, "as": "trailing"}


def _ask(grain: str, *select: dict[str, Any], **time: Any) -> dict[str, Any]:
    return {"select": list(select), "time": _time(grain, **time)}


# q20's shape: revenue by month beside the prior month's.
BESIDE_PRIOR_MONTH = _ask("month", NOW, _prior("month"))

# (name, query, key SQL): q19- and q20-shaped questions at every grain.
DIFFERENTIAL = [
    (
        "trailing_3_months",
        _ask("month", _rolling("month", 3)),
        _series_key("month", _trailing(3, "MONTH")),
    ),
    (
        "revenue_and_prior_month",
        BESIDE_PRIOR_MONTH,
        _series_key("month", f"{REVENUE_NOW}, {_revenue_at('1 MONTH')}"),
    ),
    *[
        (f"prior_{grain}", _ask(grain, _prior(grain)), _series_key(grain, _revenue_at(STEP[grain])))
        for grain in ("day", "week", "quarter", "year")
    ],
    ("trailing_7_days", _ask("day", _rolling("day", 7)), _series_key("day", _trailing(7, "DAY"))),
    (
        "prior_year_by_month",
        _ask("month", _prior("year")),
        _series_key("month", _revenue_at("1 YEAR")),
    ),
]


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return {
        name: _write_package(tmp_path_factory.mktemp(name), calendars=calendars, zone=zone)
        for name, calendars, zone in (
            ("none", (), False),
            ("authored", ("default",), False),
            ("fiscal_only", ("fiscal",), False),
            ("zoned", (), True),
            ("zoned_authored", ("default",), True),
        )
    }


@pytest.mark.parametrize(
    ("name", "query", "key"), DIFFERENTIAL, ids=[row[0] for row in DIFFERENTIAL]
)
def test_windows_without_a_calendar_match_the_key_and_an_authored_calendar(
    packages: dict[str, Path], name: str, query: dict[str, Any], key: str
) -> None:
    rows, sql = _query(packages["none"], query)
    authored_rows, authored_sql = _query(packages["authored"], query)

    assert "implicit_calendar" in sql
    assert "dim_date" in authored_sql and "implicit_calendar" not in authored_sql
    assert rows == _key(packages["none"], key)
    assert rows == authored_rows


def test_the_gap_month_is_a_zero_and_the_month_after_it_compares_with_zero(
    packages: dict[str, Path],
) -> None:
    query = BESIDE_PRIOR_MONTH
    rows, _ = _query(packages["none"], query)

    assert rows == [
        (date(2023, 11, 1), 15.0, None),
        (date(2023, 12, 1), 7.0, 15.0),
        (date(2024, 1, 1), 20.0, 7.0),
        (date(2024, 2, 1), 0.0, 20.0),
        (date(2024, 3, 1), 8.0, 0.0),
        (date(2024, 4, 1), 4.0, 8.0),
        (date(2024, 5, 1), 6.0, 4.0),
        (date(2024, 6, 1), 3.0, 6.0),
    ]


def test_groups_are_filled_across_the_whole_series(packages: dict[str, Path]) -> None:
    query = {**BESIDE_PRIOR_MONTH, "group_by": [STORE]}
    key = """
        WITH m AS (
          SELECT store_id, date_trunc('month', ordered_at) AS bucket, SUM(amount) AS revenue
          FROM orders GROUP BY 1, 2
        ),
        s AS (
          SELECT r.bucket FROM range(
            (SELECT MIN(bucket) FROM m), (SELECT MAX(bucket) FROM m) + INTERVAL 1 MONTH,
            INTERVAL 1 MONTH
          ) AS r(bucket)
        ),
        g AS (SELECT DISTINCT store_id FROM orders)
        SELECT g.store_id, s.bucket,
          COALESCE((SELECT revenue FROM m WHERE m.store_id = g.store_id AND m.bucket = s.bucket), 0),
          CASE WHEN s.bucket = (SELECT MIN(bucket) FROM s) THEN NULL ELSE COALESCE((
            SELECT revenue FROM m
            WHERE m.store_id = g.store_id AND m.bucket = s.bucket - INTERVAL 1 MONTH), 0) END
        FROM g CROSS JOIN s ORDER BY 1, 2
    """
    rows, _ = _query(packages["none"], query)
    authored_rows, _ = _query(packages["authored"], query)

    assert rows == _key(packages["none"], key)
    assert rows == authored_rows


def test_a_non_additive_fill_is_null_not_zero(packages: dict[str, Path]) -> None:
    query = _ask("month", {"expression": {**REVENUE, "aggregation": "avg"}, "as": "avg"}, fill=True)
    rows, _ = _query(packages["none"], query)

    assert (date(2024, 2, 1), None) in rows
    assert rows == _query(packages["authored"], query)[0]


@pytest.mark.parametrize(
    ("package", "time"),
    [
        # A window that starts mid-month and ends after the data: every month it touches.
        ("none", {"start": "2023-10-15", "end": "2024-08-01"}),
        # Offset bounds on a role in another zone: days in the role's zone.
        ("zoned", {"start": "2023-12-01T00:00:00-05:00", "end": "2024-04-01T04:00:00+00:00"}),
        ("zoned", {"start": "2023-11-30T23:30:00-12:00", "end": "2024-02-29T23:00:00+14:00"}),
    ],
)
def test_bounded_fill_matches_an_authored_calendar(
    packages: dict[str, Path], package: str, time: dict[str, str]
) -> None:
    query = _ask("month", NOW, fill=True, **time)
    rows, sql = _query(packages[package], query)

    assert "implicit_calendar" in sql
    assert (
        rows
        == _query(packages[f"{package}_authored" if package == "zoned" else "authored"], query)[0]
    )


def test_bounded_fill_lists_every_month_the_window_touches(packages: dict[str, Path]) -> None:
    query = _ask("month", NOW, fill=True, start="2023-10-15", end="2024-08-01")
    rows, _ = _query(packages["none"], query)

    assert [row[0] for row in rows] == [
        date(2023, 10, 1),
        *(date(2023 + (month > 12), (month - 1) % 12 + 1, 1) for month in range(11, 20)),
    ]
    assert [row[1] for row in rows] == [0.0, 15.0, 7.0, 20.0, 0.0, 8.0, 4.0, 6.0, 3.0, 0.0]


def test_zoned_role_buckets_in_its_own_zone(packages: dict[str, Path]) -> None:
    query = BESIDE_PRIOR_MONTH
    rows, _ = _query(packages["zoned"], query)
    local = "(ordered_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York'"
    key = _series_key("month", f"{REVENUE_NOW}, {_revenue_at('1 MONTH')}", local=local)

    assert rows == _key(packages["zoned"], key)
    assert rows == _query(packages["zoned_authored"], query)[0]
    # 2023-12-01 02:00 UTC is still November in New York.
    assert rows[0] == (date(2023, 11, 1), 22.0, None)


def test_a_short_generated_series_can_not_drop_data(
    packages: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    query = BESIDE_PRIOR_MONTH
    expected, _ = _query(packages["none"], query)
    original = DuckDbDialect.day_series

    def one_day_short(self: DuckDbDialect, start: Any, end: Any, source: str) -> Any:
        return original(self, start, SqlBinary(end, "-", SqlLiteral(1)), source)

    monkeypatch.setattr(DuckDbDialect, "day_series", one_day_short)
    rows, _ = _query(packages["none"], query)

    assert rows == expected


def test_no_rows_is_an_empty_answer(packages: dict[str, Path]) -> None:
    query = {
        **BESIDE_PRIOR_MONTH,
        "where": [{"field": STORE, "op": "=", "value": "no such store"}],
    }

    assert _query(packages["none"], query)[0] == []


def test_a_sentinel_date_widens_the_series_instead_of_dropping_the_row(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    query = _ask("year", NOW, _prior("year"))
    _query(package, query)  # builds the database from the seed
    with duckdb.connect(str(package / "data" / "warehouse.duckdb")) as conn:
        conn.execute("INSERT INTO orders VALUES (9, 'a', TIMESTAMP '1900-01-01 00:00:00', 1.0)")
    rows, _ = _query(package, query)

    assert len(rows) == 2024 - 1900 + 1
    assert rows[0] == (date(1900, 1, 1), 1.0, None)
    assert rows[1] == (date(1901, 1, 1), 0.0, 1.0)
    assert rows[-2:] == [(date(2023, 1, 1), 22.0, 0.0), (date(2024, 1, 1), 41.0, 22.0)]


@pytest.mark.parametrize(
    ("time", "code"),
    [
        (_time("quarter", calendar_id="fiscal"), "INCOMPATIBLE_CALENDAR"),
        (_time("quarter", calendar_id="fiscal", fill=True), "REWRITE_NOT_SUPPORTED"),
        (_time("year", calendar_id="Fiscal", fill=True), "REWRITE_NOT_SUPPORTED"),
    ],
)
def test_a_fiscal_question_without_a_fiscal_calendar_refuses(
    packages: dict[str, Path], time: dict[str, Any], code: str
) -> None:
    for select in ([NOW], [_prior("quarter")]):
        with pytest.raises(SemanticLayerError) as refused:
            _query(packages["none"], {"select": select, "time": time})
        assert refused.value.code == code


def test_a_default_question_never_borrows_a_fiscal_calendar(packages: dict[str, Path]) -> None:
    """Regression: the fiscal spine's quarters missed every Gregorian bucket and read 0."""
    query = _ask("quarter", NOW, _prior("quarter"))
    rows, sql = _query(packages["fiscal_only"], query)

    assert "dim_fiscal" not in sql
    assert rows == [
        (date(2023, 10, 1), 22.0, None),
        (date(2024, 1, 1), 28.0, 22.0),
        (date(2024, 4, 1), 13.0, 28.0),
    ]
    fiscal_rows, fiscal_sql = _query(
        packages["fiscal_only"],
        {**query, "time": _time("quarter", calendar_id="fiscal", fill=True)},
    )
    assert "dim_fiscal" in fiscal_sql
    assert [row[0] for row in fiscal_rows] == [
        date(2023, 11, 1),
        date(2024, 2, 1),
        date(2024, 5, 1),
    ]


# The implicit calendar per warehouse: a day series over dense_bounds, days cast to DATE,
# bucketed with the warehouse's own truncation.
BOUNDS = "CAST(dense_bounds.range_start AS DATE), CAST(dense_bounds.range_end AS DATE)"
START = "CAST(dense_bounds.range_start AS DATE)"
BUCKET = "DATE_TRUNC('month', CAST(implicit_days.date_day AS TIMESTAMP)) AS bucket"
DUCKDB_DAYS = (
    "CAST(day_series.series_day AS DATE) AS date_day\nFROM dense_bounds\n"
    f"CROSS JOIN LATERAL GENERATE_SERIES({BOUNDS}, INTERVAL (1) DAY) AS day_series(series_day)"
)
GOLDENS = {
    "duckdb": (DUCKDB_DAYS, BUCKET),
    "motherduck": (DUCKDB_DAYS, BUCKET),
    "ducklake": (DUCKDB_DAYS, BUCKET),
    "postgres": (
        f"{START} + day_series.day_offset AS date_day\nFROM dense_bounds\n"
        "CROSS JOIN LATERAL GENERATE_SERIES(0, CAST(dense_bounds.range_end AS DATE) - "
        f"{START}) AS day_series(day_offset)",
        BUCKET,
    ),
    "snowflake": (
        f"DATEADD(DAY, CAST(day_series.VALUE AS INTEGER), {START}) AS date_day\n"
        "FROM dense_bounds\nCROSS JOIN LATERAL FLATTEN(INPUT => ARRAY_GENERATE_RANGE(0, "
        f"DATEDIFF('day', CAST({START} AS TIMESTAMP_NTZ), "
        "CAST(CAST(dense_bounds.range_end AS DATE) AS TIMESTAMP_NTZ)) + 1)) AS day_series",
        BUCKET.replace("AS TIMESTAMP", "AS TIMESTAMP_NTZ"),
    ),
    "bigquery": (
        "series_day AS date_day\nFROM dense_bounds\n"
        f"CROSS JOIN UNNEST(GENERATE_DATE_ARRAY({BOUNDS})) AS series_day",
        "DATETIME_TRUNC(implicit_days.date_day, MONTH) AS bucket",
    ),
    "databricks": (f"EXPLODE(SEQUENCE({BOUNDS})) AS date_day\nFROM dense_bounds", BUCKET),
    "athena": (
        "day_series.series_day AS date_day\nFROM dense_bounds\n"
        f"CROSS JOIN UNNEST(SEQUENCE({BOUNDS})) AS day_series(series_day)",
        BUCKET,
    ),
}


@pytest.fixture(scope="module")
def no_calendar_config(packages: dict[str, Path]) -> Any:
    return load_package_config(str(packages["none"]))


@pytest.mark.parametrize("warehouse", [*GOLDENS, "clickhouse", "generic"])
def test_each_warehouse_generates_a_bounded_day_spine_or_refuses(
    no_calendar_config: Any, warehouse: str
) -> None:
    config = replace(
        no_calendar_config, package=replace(no_calendar_config.package, warehouse=warehouse)
    )
    query = {"version": 1, **BESIDE_PRIOR_MONTH}
    if warehouse not in GOLDENS:
        with pytest.raises(SemanticLayerError, match="has no implicit calendar"):
            compile_query(config, Registry(config), query)
        return
    sql = compile_query(config, Registry(config), query)["sql"]
    days, bucket = GOLDENS[warehouse]

    assert (
        f"implicit_days AS (\nSELECT\n  {days}\n),\nimplicit_calendar AS (\nSELECT\n"
        f"  implicit_days.date_day AS date_day,\n  {bucket}\nFROM implicit_days\n)"
    ) in sql, sql


@pytest.mark.parametrize(
    ("start", "end", "first", "last"),
    [
        ("0001-01-02", "0001-03-01", "0001-01-01", "0001-03-04"),
        ("9999-12-01", "9999-12-31T12:00:00", "9999-11-28", "9999-12-31"),
    ],
)
def test_bounded_padding_is_clamped_to_the_calendar(
    no_calendar_config: Any, start: str, end: str, first: str, last: str
) -> None:
    query = {"version": 1, **_ask("month", NOW, fill=True, start=start, end=end)}
    sql = compile_query(no_calendar_config, Registry(no_calendar_config), query)["sql"]

    assert f"CAST('{first}' AS DATE) AS range_start" in sql
    assert f"CAST('{last}' AS DATE) AS range_end" in sql
