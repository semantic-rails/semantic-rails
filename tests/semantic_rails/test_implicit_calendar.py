"""The implicit Gregorian calendar: rolling, prior_period and time.fill with no authored calendar.

Every answer is checked two ways on DuckDB: against an independent key (calendar arithmetic over
a bucket series, not the engine's row-counted windows over a day spine), and against the same
package with an authored calendar, which must give identical rows.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.ast import PERIOD_PARTIAL
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.dialects import DuckDbDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata_parts.capabilities import _capability_payload
from semantic_rails.query_call import read_call
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
FISCAL_DAY = "temporal_role.cal_fiscal_calendar_date_day"
REVENUE = "measure.cal.revenue"
STORE = "dimension.cal_order_store_id"
STEP = {"day": "1 DAY", "week": "7 DAY", "month": "1 MONTH", "quarter": "3 MONTH", "year": "1 YEAR"}


PACKAGE = """
schema_version: 1
package: {id: cal, namespace: cal, warehouse: duckdb, default_db: data/warehouse.duckdb,
  seed: {kind: sql_script, source: data/seed.sql}}
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


def _query(
    package: Path, query: dict[str, Any], *, internal: dict[str, Any] | None = None
) -> tuple[list[tuple[Any, ...]], str]:
    """Rows as sorted tuples (dates for buckets, floats for amounts) and the rendered SQL.

    ``internal`` replaces parts of the call's engine form with shapes no call can spell.
    """
    engine = Runtime.from_path(str(package))
    try:
        if internal:
            result = engine.query_internal({**engine.engine_payload(query), **internal})
        else:
            result = engine.query(query)
    finally:
        engine.close()
    rows = [{k: v for k, v in row.items() if k != PERIOD_PARTIAL} for row in result["rows"]]
    return _sorted(
        _normal(tuple(row.values()), [result["column_types"][key]["type"] for key in row])
        for row in rows
    ), str(result["rendered_sql"])


def _key(package: Path, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        return _sorted(_normal(row) for row in conn.execute(sql).fetchall())


def _sorted(rows: Any) -> list[tuple[Any, ...]]:
    """By the key columns (the group, then the bucket), which are unique per row."""
    return sorted(rows, key=lambda row: [str(value) for value in row[:2]])


def _normal(row: tuple[Any, ...], types: list[str] | None = None) -> tuple[Any, ...]:
    def one(value: Any, kind: str) -> Any:
        if value is not None and kind == "decimal":
            value = Decimal(value)
        if value is not None and kind in {"date", "timestamp"}:
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, (Decimal, float)):
            return round(float(value), 6)
        return value

    return tuple(one(value, types[index] if types else "") for index, value in enumerate(row))


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
    built = {
        name: _write_package(tmp_path_factory.mktemp(name), calendars=calendars, zone=zone)
        for name, calendars, zone in (
            ("none", (), False),
            ("authored", ("default",), False),
            ("fiscal_only", ("fiscal",), False),
            ("zoned", (), True),
            ("zoned_authored", ("default",), True),
        )
    }
    for package in built.values():  # seed the warehouse `_key` reads, whatever test runs first
        _query(package, _ask("month", NOW))
    return built


@pytest.mark.parametrize(
    ("name", "query", "key"), DIFFERENTIAL, ids=[row[0] for row in DIFFERENTIAL]
)
def test_windows_without_a_calendar_match_the_key_and_an_authored_calendar(
    packages: dict[str, Path], name: str, query: dict[str, Any], key: str
) -> None:
    rows, sql = _query(packages["none"], query)
    authored_rows, authored_sql = _query(packages["authored"], query)

    assert "implicit_calendar" in sql
    # An authored default calendar takes no part in the series: the same implicit spine.
    assert "dim_date" not in authored_sql and "implicit_calendar" in authored_sql
    assert rows == _key(packages["none"], key)
    assert rows == authored_rows


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
    avg = {"kind": "measure", "measure": REVENUE, "aggregation": "avg"}
    query = _ask("month", {"expression": avg, "as": "avg"}, fill=True)
    rows, _ = _query(packages["none"], query)

    assert (date(2024, 2, 1), None) in rows
    assert rows == _query(packages["authored"], query)[0]


@pytest.mark.parametrize(
    ("package", "time", "group_by"),
    [
        # A window that starts mid-month and ends after the data: every month it touches.
        ("none", {"start": "2023-10-15", "end": "2024-08-01"}, []),
        ("none", {"start": "2023-12-01", "end": "2024-04-01"}, [STORE]),
        # Offset bounds on a role in another zone: days in the role's zone.
        ("zoned", {"start": "2023-12-01T00:00:00-05:00", "end": "2024-04-01T04:00:00+00:00"}, []),
        ("zoned", {"start": "2023-11-30T23:30:00-12:00", "end": "2024-02-29T23:00:00+14:00"}, []),
    ],
)
def test_bounded_fill_matches_an_authored_calendar(
    packages: dict[str, Path], package: str, time: dict[str, str], group_by: list[str]
) -> None:
    query = {**_ask("month", NOW, **time), "group_by": group_by}
    rows, sql = _query(packages[package], query)

    assert "implicit_calendar" in sql
    assert (
        rows
        == _query(packages[f"{package}_authored" if package == "zoned" else "authored"], query)[0]
    )


def test_bounded_fill_lists_every_month_the_window_touches(packages: dict[str, Path]) -> None:
    query = _ask("month", NOW, start="2023-10-15", end="2024-08-01")
    rows, _ = _query(packages["none"], query)

    assert [row[0] for row in rows] == [
        date(2023, 10, 1),
        *(date(2023 + (month > 12), (month - 1) % 12 + 1, 1) for month in range(11, 20)),
    ]
    assert [row[1] for row in rows] == [None, 15.0, 7.0, 20.0, 0.0, 8.0, 4.0, 6.0, 3.0, None]


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
    """Even a day spine one day short answers every month: the gap month is a zero and the
    month after it compares with zero."""
    expected = [
        (date(2023, 11, 1), 15.0, None),
        (date(2023, 12, 1), 7.0, 15.0),
        (date(2024, 1, 1), 20.0, 7.0),
        (date(2024, 2, 1), 0.0, 20.0),
        (date(2024, 3, 1), 8.0, 0.0),
        (date(2024, 4, 1), 4.0, 8.0),
        (date(2024, 5, 1), 6.0, 4.0),
        (date(2024, 6, 1), 3.0, 6.0),
    ]
    assert _query(packages["none"], BESIDE_PRIOR_MONTH)[0] == expected
    original = DuckDbDialect.day_series

    def one_day_short(self: DuckDbDialect, start: Any, end: Any, source: str) -> Any:
        return original(self, start, SqlBinary(end, "-", SqlLiteral(1)), source)

    monkeypatch.setattr(DuckDbDialect, "day_series", one_day_short)

    assert _query(packages["none"], BESIDE_PRIOR_MONTH)[0] == expected


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


def test_a_call_may_not_name_a_calendar(packages: dict[str, Path]) -> None:
    """The clock's own calendar applies; a call naming another one is refused with the fix."""
    with pytest.raises(SemanticLayerError) as refused:
        _query(packages["fiscal_only"], _ask("quarter", NOW, calendar_id="fiscal"))

    assert refused.value.code == "INVALID_QUERY"
    assert refused.value.details["path"] == "time.calendar_id"
    assert refused.value.details["recovery_hints"] == [
        {"code": "FIX_QUERY_CALL", "message": "Remove 'calendar_id'."}
    ]


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
    with pytest.raises(SemanticLayerError) as refused:
        _query(packages["fiscal_only"], _ask("quarter", NOW, temporal_role=FISCAL_DAY, fill=True))
    assert refused.value.details["reason"] == "calendar_not_supported_yet"


DISTRIBUTION = {
    "expression": {
        "kind": "distribution",
        "function": "percentile",
        "p": 0.8,
        "over": {"kind": "entity_value", "entity": "entity.cal_order", "input": REVENUE},
    },
    "as": "p80",
}
# An order's revenue in each row's own context: the engine's form, which no call spells.
LARGE_ORDER = {
    "kind": "metric_predicate",
    "entity": "entity.cal_order",
    "scope_mode": "contextual",
    "input": {"measure": REVENUE},
    "op": ">",
    "value": 5,
}
LARGE_ORDERS = {"metric_filters": [{"expression": LARGE_ORDER, "op": "=", "value": True}]}


def test_a_query_compiled_as_sub_queries_holds_the_implicit_calendar(
    packages: dict[str, Path],
) -> None:
    # Each part is namespaced, the implicit calendar's generated day series included; an
    # authored default calendar takes no part, so both packages compile the same series.
    query = _ask("month", DISTRIBUTION, _rolling("month", 3))
    rows, sql = _query(packages["none"], query)
    authored_rows, authored_sql = _query(packages["authored"], query)

    assert "implicit_calendar" in sql and "dim_date" not in authored_sql
    assert rows == authored_rows


def _distribution(function: str, input_: dict[str, Any], **extra: Any) -> dict[str, Any]:
    over = {"kind": "entity_value", "entity": "entity.cal_order", "input": input_, **extra}
    expression = {"kind": "distribution", "function": function, "over": over}
    return {"expression": {**expression, "p": 0.8} if function == "percentile" else expression}


def _per_order(aggregate: str) -> str:
    """``aggregate`` over the bucket's orders (one row per order); NULL when it has none."""
    return f"(SELECT {aggregate} FROM orders WHERE date_trunc('month', ordered_at) = s.bucket)"


LARGE_REVENUE = {"kind": "aggregate", "measure": "measure.cal.revenue"}
LARGE_REVENUE["filter"] = {"all": [{"expression": LARGE_ORDER}]}
MEDIAN_LARGE = _distribution("median", LARGE_REVENUE)


# Regression: fill densified the per-order grain, so every order entered every month as a 0
# (p80 read 3.0, 0, 0 for 9, 7, 20). A filled distribution is refused; a named window fills
# the distribution's answer instead, and refuses it beside a sum.
@pytest.mark.parametrize(
    ("package", "select", "time", "internal"),
    [
        ("authored", [DISTRIBUTION], {"fill": True}, None),
        ("authored", [NOW, DISTRIBUTION], {"start": "2023-10-15", "end": "2024-08-01"}, None),
        ("authored", [DISTRIBUTION], {"fill": True}, LARGE_ORDERS),
        ("authored", [], {"fill": True}, {"select": [{**MEDIAN_LARGE, "as": "median"}]}),
        ("none", [DISTRIBUTION, NOW], {"fill": True}, None),
        ("fiscal_only", [DISTRIBUTION], {"temporal_role": FISCAL_DAY, "fill": True}, None),
    ],
    ids=[
        "authored",
        "beside",
        "metric_predicate",
        "input_predicate",
        "none_beside",
        "fiscal",
    ],
)
def test_a_filled_distribution_refuses(
    packages: dict[str, Path],
    package: str,
    select: list[dict[str, Any]],
    time: dict[str, Any],
    internal: dict[str, Any] | None,
) -> None:
    with pytest.raises(SemanticLayerError) as refused:
        _query(packages[package], _ask("month", *select, **time), internal=internal)

    assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    if package == "fiscal_only":
        assert refused.value.details["reason"] == "calendar_not_supported_yet"
    elif "fill" in time:
        assert "time.fill isn't supported for distributions" in str(refused.value)
    else:
        # A named window fills the combined answer, where a sum's empty period can't tell 0
        # from no data yet: a distribution beside one is asked for separately.
        assert refused.value.details["reason"] == "distribution_beside_additive_in_named_window"


@pytest.mark.parametrize(
    ("function", "aggregate", "package"),
    [
        ("median", "median(amount)", "authored"),
        ("percentile", "quantile_cont(amount, 0.8)", "none"),
        ("avg", "avg(amount)", "authored"),
        ("sum", "sum(amount)", "none"),
        ("min", "min(amount)", "authored"),
        ("max", "max(amount)", "none"),
    ],
)
def test_an_unfilled_distribution_matches_the_key(
    packages: dict[str, Path], function: str, aggregate: str, package: str
) -> None:
    select = {**_distribution(function, REVENUE), "as": "value"}
    key = _key(packages["authored"], _series_key("month", _per_order(aggregate)))

    assert _query(packages["authored"], _ask("month", select))[0] == [
        row for row in key if row[1] is not None
    ]
    # A prior-period sibling fills its own branch; the distribution reads NULL in February.
    beside = _ask("month", _prior("month"), select)
    assert _query(packages[package], beside)[0] == _key(
        packages[package],
        _series_key("month", f"{_revenue_at('1 MONTH')}, {_per_order(aggregate)}"),
    )


def test_a_named_window_answers_every_period_of_a_distribution(packages: dict[str, Path]) -> None:
    """February has no order: the window still answers it, and its median reads NULL, not 0."""
    select = {**_distribution("median", REVENUE), "as": "value"}
    rows, _ = _query(
        packages["authored"], _ask("month", select, start="2023-12-01", end="2024-04-01")
    )
    months = "range(TIMESTAMP '2023-12-01', TIMESTAMP '2024-04-01', INTERVAL 1 MONTH) AS s(bucket)"
    key = f"SELECT s.bucket, {_per_order('median(amount)')} FROM {months}"

    assert rows == _key(packages["authored"], key)
    assert (date(2024, 2, 1), None) in rows


PRIOR_REVENUE = _prior("month")["expression"]
MEDIAN_OF = {
    "rolling": _distribution("median", _rolling("month", 3)["expression"]),
    "prior_period": _distribution("median", PRIOR_REVENUE),
    "prior_period_filter": _distribution("median", REVENUE),
}


@pytest.mark.parametrize("shape", MEDIAN_OF)
def test_a_distribution_over_a_per_entity_window_refuses(
    packages: dict[str, Path], shape: str
) -> None:
    """Its dense series held every order in every month: November's median read 0, not 7.5."""
    query = _ask("month", {**MEDIAN_OF[shape], "as": "value"})
    if shape == "prior_period_filter":
        query["select"].append(_prior("month"))
        query["having"] = [{"field": "prior", "op": "gte", "value": 0}]
    with pytest.raises(SemanticLayerError) as refused:
        _query(packages["authored"], query)

    assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    assert "prior-period window" in str(refused.value)


@pytest.mark.parametrize(
    "query",
    [
        _ask("month", NOW, start="2023-10-15", end="2024-08-01"),
        _ask("month", NOW, _prior("month")),
    ],
)
def test_a_metric_predicate_matches_an_authored_calendar(
    packages: dict[str, Path], query: dict[str, Any]
) -> None:
    rows, sql = _query(packages["none"], query, internal=LARGE_ORDERS)

    assert "implicit_calendar" in sql
    assert rows == _query(packages["authored"], query, internal=LARGE_ORDERS)[0]


@pytest.mark.parametrize(
    "name", ["EXPLODE", "sequence", "Generate_Date_Array", "ARRAY_GENERATE_RANGE"]
)
def test_a_call_may_not_name_the_calendar_generators(packages: dict[str, Path], name: str) -> None:
    call = {"expression": {"kind": "call", "name": name, "args": [REVENUE]}, "as": "rows"}
    with pytest.raises(SemanticLayerError) as refused:
        _query(packages["none"], _ask("month", call))

    assert refused.value.code == "INVALID_EXPRESSION_AST"


@pytest.mark.parametrize(
    ("package", "warehouse", "available"),
    [("none", "duckdb", True), ("fiscal_only", "clickhouse", False)],
)
def test_dense_fill_capability_follows_the_calendar_that_would_fill(
    packages: dict[str, Path], package: str, warehouse: str, available: bool
) -> None:
    config = load_package_config(str(packages[package]))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    supported, _ = _capability_payload(config)

    assert ("dense_fill" in {row["kind"] for row in supported}) is available


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


def _compile(config: Any, query: dict[str, Any], *, fill: bool) -> dict[str, Any]:
    """The compiled call; ``fill`` asks the engine itself to fill the window."""
    payload, _ = read_call(query, config=config)
    payload["time"]["fill"] = fill
    return compile_query(config, Registry(config), payload)


@pytest.mark.parametrize(
    ("warehouse", "bounded"),
    [
        ("duckdb", False),
        ("duckdb", True),
        ("postgres", False),
        ("snowflake", True),
        ("bigquery", False),
        ("databricks", True),
        # Athena's statement preparation types the bounds' literals as TIMESTAMP.
        ("athena", True),
        ("clickhouse", False),
        ("generic", True),
    ],
)
def test_each_warehouse_generates_a_bounded_day_spine_or_refuses(
    no_calendar_config: Any, warehouse: str, bounded: bool
) -> None:
    config = replace(
        no_calendar_config, package=replace(no_calendar_config.package, warehouse=warehouse)
    )
    window = {"start": "2023-10-15", "end": "2024-08-01"}
    query = _ask("month", NOW, **window) if bounded else BESIDE_PRIOR_MONTH
    if warehouse not in GOLDENS:
        with pytest.raises(SemanticLayerError) as refused:
            _compile(config, query, fill=bounded)
        assert refused.value.code == "REWRITE_NOT_SUPPORTED"
        assert refused.value.details == {"warehouse": warehouse, "rewrite": "day_series"}
        return
    sql = _compile(config, query, fill=bounded)["sql"]
    days, bucket = GOLDENS[warehouse]

    assert (
        f"implicit_days AS (\nSELECT\n  {days}\n),\nimplicit_calendar AS (\nSELECT\n"
        f"  implicit_days.date_day AS date_day,\n  {bucket}\nFROM implicit_days\n)"
    ) in sql, sql
    if bounded:  # the window's days, widened by three and narrowed by the day predicate
        assert (
            "dense_bounds AS (\nSELECT\n  CAST('2023-10-12' AS DATE) AS range_start,\n"
            "  CAST('2024-08-04' AS DATE) AS range_end\n)"
        ) in sql, sql
        assert re.search(r"implicit_calendar\.date_day >= (TIMESTAMP )?'2023-10-15'", sql), sql
        assert re.search(r"implicit_calendar\.date_day < (TIMESTAMP )?'2024-08-01'", sql), sql


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
    query = _ask("month", NOW, start=start, end=end)
    sql = _compile(no_calendar_config, query, fill=True)["sql"]

    assert f"CAST('{first}' AS DATE) AS range_start" in sql
    assert f"CAST('{last}' AS DATE) AS range_end" in sql
