"""A distribution query answers each select item in its own sub-query, then joins them on the keys.

The items disagree on the period's type: a rolling or prior_period item reads it from the
calendar's DATE column, the others from DATE_TRUNC's timestamp. Postgres and BigQuery join keys
by their text, where the two never match, so the period is cast to one type before the join.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry
from tests.semantic_rails.test_implicit_calendar import (
    DISTRIBUTION,
    NOW,
    STORE,
    _ask,
    _normal,
    _prior,
    _query,
    _rolling,
    _sorted,
    _write_package,
)

SHAPES = {
    "beside_rolling": (DISTRIBUTION, _rolling("month", 3)),
    "beside_prior": (DISTRIBUTION, _prior("month")),
    "after_rolling": (_rolling("month", 3), DISTRIBUTION),
    "beside_both": (DISTRIBUTION, _rolling("month", 3), _prior("month")),
}
# The timestamp each warehouse's DATE_TRUNC returns.
PERIOD_TYPE = {
    "postgres": "TIMESTAMP",
    "bigquery": "DATETIME",
    "duckdb": "TIMESTAMP",
    "snowflake": "TIMESTAMP_NTZ",
    "databricks": "TIMESTAMP",
    "athena": "TIMESTAMP",
    "clickhouse": "TIMESTAMP",
}


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    built = {
        name: _write_package(tmp_path_factory.mktemp(name), calendars=("default",), zone=zone)
        for name, zone in (("authored", False), ("zoned", True))
    }
    for package in built.values():  # seed the warehouse the Postgres SQL runs on
        _query(package, _ask("month", NOW))
    return built


def _sql(package: Path, warehouse: str, query: dict[str, Any]) -> str:
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    return str(compile_query(config, Registry(config), {"version": 1, **query})["sql"])


def _run_postgres_sql(package: Path, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Postgres's SQL, run by DuckDB, which writes DATE and TIMESTAMP as text as Postgres does.

    The session zone is far from UTC: the period's cast must not move it.
    """
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        conn.execute("SET TimeZone = 'Pacific/Kiritimati'")
        rows = conn.execute(_sql(package, "postgres", query)).fetchall()
    return _sorted(_normal(row) for row in rows)


@pytest.mark.parametrize("package", ["authored", "zoned"])
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("shape", SHAPES)
def test_postgres_answers_each_period_once(
    packages: dict[str, Path], shape: str, grouped: bool, package: str
) -> None:
    query = _ask("month", *SHAPES[shape])
    if grouped:
        query["group_by"] = [STORE]
    expected, _ = _query(packages[package], query)

    rows = _run_postgres_sql(packages[package], query)

    assert rows == expected
    keys = [row[: 2 if grouped else 1] for row in rows]
    assert len(keys) == len(set(keys)), rows


def test_the_distribution_beside_the_prior_month_is_the_key(packages: dict[str, Path]) -> None:
    # p80 of each order's revenue; February has no orders, so no p80 and a prior of 20.
    key = [
        (date(2023, 11, 1), 9.0, None),
        (date(2023, 12, 1), 7.0, 15.0),
        (date(2024, 1, 1), 20.0, 7.0),
        (date(2024, 2, 1), None, 20.0),
        (date(2024, 3, 1), 8.0, 0.0),
        (date(2024, 4, 1), 4.0, 8.0),
        (date(2024, 5, 1), 6.0, 4.0),
        (date(2024, 6, 1), 3.0, 6.0),
    ]

    assert _run_postgres_sql(packages["authored"], _ask("month", *SHAPES["beside_prior"])) == key


@pytest.mark.parametrize("warehouse", PERIOD_TYPE)
def test_every_warehouse_joins_the_items_on_the_period_as_its_date_trunc_type(
    packages: dict[str, Path], warehouse: str
) -> None:
    sql = _sql(packages["authored"], warehouse, _ask("month", *SHAPES["beside_both"]))
    # BigQuery renames the alias to temporal_role_cal_order_ordered_at__month_<hash>.
    period = (
        r"(CAST\()?(left_side|right_side)\.[`\"]?temporal_role[._]cal_order_ordered_at__month\w*"
    )
    refs = re.findall(period + r"[`\"]?( AS (\w+)\))?", sql)

    # Two joins; each casts the period on both sides of its COALESCE and join condition.
    assert len(refs) >= 8, sql
    assert {(cast, type_name) for cast, _, _, type_name in refs} == {
        ("CAST(", PERIOD_TYPE[warehouse])
    }, sql
