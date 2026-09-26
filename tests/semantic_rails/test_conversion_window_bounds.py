"""A conversion window is a duration after the base event: ``base <= converted < base + N``.

Each edge case compiles an ad-hoc conversion (one base event, one converted event) for
jaffle_shop and executes the SQL on DuckDB, through both lowering paths: the plain rate
and the converted-side dimension binding. The golden table pins the window predicate
every other dialect renders.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.dialects import supported_warehouses
from semantic_rails.registry import Registry

_CONFIG = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
_BINDING = {
    "dimension.jaffle_product_type": {"side": "converted", "denominator": "all_base_events"}
}

_T0 = "2024-01-31 10:00:00"
# id, window, base event, converted event, converts. Date-only times use DATE columns.
EDGE_CASES = [
    ("day-at-base-instant", "7 day", _T0, _T0, True),
    ("day-just-before-base", "7 day", _T0, "2024-01-31 09:59:59", False),
    ("day-just-before-7x24h", "7 day", _T0, "2024-02-07 09:59:59", True),
    ("day-exactly-7x24h", "7 day", _T0, "2024-02-07 10:00:00", False),
    ("day-just-after-7x24h", "7 day", _T0, "2024-02-07 10:00:01", False),
    ("day-same-calendar-day-past-7x24h", "7 day", _T0, "2024-02-07 23:59:59", False),
    ("hour-just-before-60min", "1 hour", _T0, "2024-01-31 10:59:59", True),
    ("hour-exactly-60min", "1 hour", _T0, "2024-01-31 11:00:00", False),
    ("hour-same-clock-hour-past-60min", "1 hour", _T0, "2024-01-31 11:30:00", False),
    ("week-just-before-7x24h", "1 week", _T0, "2024-02-07 09:59:59", True),
    ("week-8-days", "1 week", _T0, "2024-02-08 10:00:00", False),
    ("month-just-before-clamped-end", "1 month", _T0, "2024-02-29 09:59:59", True),
    ("month-exactly-clamped-end", "1 month", _T0, "2024-02-29 10:00:00", False),
    ("date-6-days-later", "7 day", "2024-01-31", "2024-02-06", True),
    ("date-7-days-later", "7 day", "2024-01-31", "2024-02-07", False),
]


def _query(window: str, *, bound: bool) -> dict[str, Any]:
    value, unit = window.split()
    expression: dict[str, Any] = {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": unit, "value": int(value)},
        "matching_mode": "first_converted_after_base",
        "base": {"kind": "aggregate", "measure": "measure.jaffle.session_starts"},
        "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
    }
    query: dict[str, Any] = {"version": 2, "select": [{"as": "rate", "expression": expression}]}
    if bound:
        expression["dimension_bindings"] = _BINDING
        query["group_by"] = ["dimension.jaffle_product_type"]
    return query


@pytest.mark.parametrize("bound", [False, True], ids=["rate", "converted-side-binding"])
@pytest.mark.parametrize(
    ("case", "window", "base_at", "converted_at", "converts"),
    EDGE_CASES,
    ids=[case[0] for case in EDGE_CASES],
)
def test_duckdb_conversion_window_is_a_half_open_duration(
    case: str, window: str, base_at: str, converted_at: str, converts: bool, bound: bool
) -> None:
    sql = compile_query(_CONFIG, Registry(_CONFIG), _query(window, bound=bound))["sql"]
    column_type = "DATE" if len(base_at) == len("2024-01-31") else "TIMESTAMP"
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TABLE jaffle_customer AS SELECT 'c1' AS customer_id;
        CREATE TABLE jaffle_storefront_session AS
          SELECT 's1' AS session_id, 'c1' AS customer_id, CAST('{base_at}' AS {column_type}) AS started_at;
        CREATE TABLE jaffle_order AS
          SELECT 'o1' AS order_id, 'c1' AS customer_id, CAST('{converted_at}' AS {column_type}) AS ordered_at;
        CREATE TABLE jaffle_item AS SELECT 'o1' AS order_id, 'k1' AS sku;
        CREATE TABLE jaffle_product AS SELECT 'k1' AS sku, 'beverage' AS product_type;
        """
    )
    rates = [row[-1] for row in con.execute(sql).fetchall()]
    # The binding path emits a row only for a converted-side value that converted.
    assert rates == ([1.0] if converts else [] if bound else [0.0])


_LOWER = "converted_events.__converted_event_time >= base_events.__base_event_time"
_BASE_TS = "CAST(base_events.__base_event_time AS TIMESTAMP)"
_CONVERTED_TS = "CAST(converted_events.__converted_event_time AS TIMESTAMP)"
_DUCKDB_UPPER = f"{_CONVERTED_TS} < DATE_ADD({_BASE_TS}, INTERVAL (7) DAY)"
WINDOW_PREDICATE_GOLDENS = {
    "athena": f"{_CONVERTED_TS} < DATE_ADD('day', 7, {_BASE_TS})",
    "bigquery": (
        "converted_events.__converted_event_time"
        " < DATETIME_ADD(base_events.__base_event_time, INTERVAL (7) DAY)"
    ),
    "clickhouse": _DUCKDB_UPPER,
    "databricks": f"{_CONVERTED_TS} < TIMESTAMPADD(DAY, 7, {_BASE_TS})",
    "duckdb": _DUCKDB_UPPER,
    "ducklake": _DUCKDB_UPPER,
    "motherduck": _DUCKDB_UPPER,
    "postgres": (
        f"{_CONVERTED_TS} < CAST({_BASE_TS} AS TIMESTAMP)"
        " + 7 * (CAST('2000-01-02' AS TIMESTAMP) - CAST('2000-01-01' AS TIMESTAMP))"
    ),
    "snowflake": (
        "CAST(converted_events.__converted_event_time AS TIMESTAMP_NTZ)"
        " < DATEADD(DAY, 7, CAST(base_events.__base_event_time AS TIMESTAMP_NTZ))"
    ),
}


def test_window_predicate_goldens_cover_every_dialect() -> None:
    assert sorted(WINDOW_PREDICATE_GOLDENS) == sorted(supported_warehouses())


@pytest.mark.parametrize("bound", [False, True], ids=["rate", "converted-side-binding"])
@pytest.mark.parametrize("warehouse", sorted(WINDOW_PREDICATE_GOLDENS))
def test_every_dialect_renders_the_duration_window(warehouse: str, bound: bool) -> None:
    config = replace(_CONFIG, package=replace(_CONFIG.package, warehouse=warehouse))
    sql = compile_query(config, Registry(config), _query("7 day", bound=bound))["sql"]
    assert f"ON {_LOWER} AND {WINDOW_PREDICATE_GOLDENS[warehouse]} AND " in sql
