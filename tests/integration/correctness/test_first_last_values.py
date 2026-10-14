"""A first or last value skips a row whose value or order key is NULL, as DuckDB's ``arg_min``
and ``arg_max`` do, on Postgres too.

The shop's earliest and latest orders get no amount, and one more order no time: the first
value is the earliest order with an amount, the last value the latest one, and an order with
no time is neither. A package's flow measure allows neither aggregation, so revenue allows
both here; the dialects' SQL is what is compared.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, SeedSpec

from .conftest import _rows, _strict, _write_variant

REVENUE = "measure.shop.revenue"
LAST_SEEDED = "(11, 108, 'a', TIMESTAMP '2024-08-01 02:00:00', DATE '2024-08-01', 2.00)"
NULL_ROWS = (
    ",\n  (12, 101, 'a', TIMESTAMP '2023-01-01 00:00:00', DATE '2023-01-01', NULL)"
    ",\n  (13, 102, 'b', NULL, NULL, 50.00)"
    ",\n  (14, 103, 'a', TIMESTAMP '2024-12-01 00:00:00', DATE '2024-12-01', NULL)"
)
CALL = {
    "select": [
        {"as": edge, "expression": {"kind": "measure", "measure": REVENUE, "aggregation": agg}}
        for edge, agg in (("first", "first_value"), ("last", "last_value"))
    ]
}
# Order 1 (2023-11-03, 10.00) is the earliest with an amount, order 11 (2024-08-01, 2.00) the
# latest.
EXPECTED: tuple[Decimal | None, ...] = (Decimal("10.00"), Decimal("2.00"))


@pytest.fixture
def package(tmp_path: Path) -> Path:
    package = _write_variant(tmp_path, "utc_authored")
    seed = package / "data" / "seed.sql"
    text = seed.read_text(encoding="utf-8")
    assert text.count(LAST_SEEDED) == 1
    seed.write_text(text.replace(LAST_SEEDED, LAST_SEEDED + NULL_ROWS), encoding="utf-8")
    return package


def _runtime(package: Path, **overrides) -> Runtime:
    config = load_package_config(str(package))
    measures = [
        replace(
            measure,
            allowed_aggregations=[*measure.allowed_aggregations, "first_value", "last_value"],
            invalid_aggregations=[
                agg
                for agg in measure.invalid_aggregations
                if agg not in {"first_value", "last_value"}
            ],
        )
        if measure.id == REVENUE
        else measure
        for measure in config.measures
    ]
    config = replace(config, measures=measures, package=replace(config.package, **overrides))
    return Runtime.from_config(config, source_path=str(package), package_id="shop")


def _answer(runtime: Runtime) -> tuple[Decimal | None, ...]:
    (row,) = runtime.query(CALL)["rows"]
    return tuple(
        None if row[edge] is None else Decimal(str(row[edge])) for edge in ("first", "last")
    )


@pytest.fixture
def postgres(package: Path) -> Iterator[Runtime]:
    """The seed with its NULL rows in a Postgres schema of its own, dropped afterwards."""
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    if any(not os.environ.get(name, "").strip() for name in required):
        pytest.skip("postgres: SR_POSTGRES_* unset (run under with_postgres.sh)")
    schema = f"sr_first_last_{uuid.uuid4().hex[:12]}"
    options = {
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    }

    def connect(**extra: str) -> Runtime:
        return _runtime(
            package,
            warehouse="postgres",
            default_db="",
            seed=SeedSpec(),
            connection=ConnectionSpec(
                kind="postgres_native", name="", options={**options, **extra}
            ),
        )

    admin = connect()
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__})")
    runtime = connect(schema=schema)
    try:
        _rows(runtime, (package / "data" / "seed.sql").read_text(encoding="utf-8"))
        yield runtime
    finally:
        runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def test_duckdb_first_and_last_values_skip_rows_without_a_value_or_a_time(package: Path):
    runtime = _runtime(package)
    try:
        reference = _rows(
            runtime,
            "SELECT arg_min(amount, ordered_at), arg_max(amount, ordered_at) FROM orders",
        )
        assert tuple(Decimal(str(value)) for value in reference[0]) == EXPECTED
        assert _answer(runtime) == EXPECTED
    finally:
        runtime.close()


def test_postgres_first_and_last_values_match_duckdb(postgres: Runtime):
    assert _answer(postgres) == EXPECTED
