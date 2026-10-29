"""Backends for the differential correctness suite.

Each backend runs the ``shop`` package over the same seed (``shop/data/seed.sql``, written
in SQL that DuckDB and Postgres both accept). DuckDB always runs. Postgres runs when the
``SR_POSTGRES_*`` variables name a server (``make test-postgres`` starts a throwaway one)
and otherwise skips; ``SR_INTEGRATION_STRICT=1`` turns an unreachable server into a failure.
The seed goes into a schema of its own, dropped afterwards, so a shared server is safe. The
``vendors`` fixture runs the ``vendors`` package the same way, on each backend in turn.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.db import _split_sql_statements
from semantic_rails.db_parts.adbc import (
    _check_postgres_result_types,
    _check_postgres_result_values,
    _postgres_value,
)
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, SeedSpec

SHOP = Path(__file__).resolve().parent / "shop"
SEED = (SHOP / "data" / "seed.sql").read_text(encoding="utf-8")
CALENDARS = ("default_calendar", "fiscal_calendar")
# Answers must not depend on the session time zone, so every session uses one that is
# neither UTC (the package's zone) nor the zone the package converts to.
SESSION_ZONE = "America/Los_Angeles"


@dataclass
class Backend:
    name: str
    runtimes: dict[str, Runtime]  # by package variant (VARIANTS)
    reference: Callable[[str], list[tuple[Any, ...]]]


def _strict() -> bool:
    return os.environ.get("SR_INTEGRATION_STRICT", "").strip() in {"1", "true", "yes"}


# How the order clock reads: the UTC timestamp as stored, that timestamp in New York, the
# DATE column, or the zone-aware column. Each runs with the authored calendars and without.
CLOCKS: dict[str, dict[str, str]] = {
    "utc": {},
    "ny": {"timezone": "America/New_York", "column_timezone": "UTC"},
    "date": {"column": "order_date", "kind": "date"},
    "tz": {"column": "ordered_at_tz"},
}
VARIANTS = tuple(f"{clock}_{calendar}" for clock in CLOCKS for calendar in ("authored", "implicit"))


def _write_variant(root: Path, variant: str) -> Path:
    clock, calendar = variant.split("_")
    package = root / "shop"
    shutil.copytree(SHOP, package)
    orders = package / "models" / "orders.yml"
    model = yaml.safe_load(orders.read_text(encoding="utf-8"))
    model["model"]["times"]["ordered_at"].update(CLOCKS[clock])
    orders.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    if calendar == "implicit":
        graph = yaml.safe_load((package / "graph.yml").read_text(encoding="utf-8"))
        for name in CALENDARS:
            (package / "models" / f"{name}.yml").unlink()
            del graph["graph"]["entities"][name]
        (package / "graph.yml").write_text(yaml.safe_dump(graph), encoding="utf-8")
    return package


@pytest.fixture(scope="session")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return {name: _write_variant(tmp_path_factory.mktemp(name), name) for name in VARIANTS}


def _runtime(package: Path, **overrides: Any) -> Runtime:
    config = load_package_config(str(package))
    if overrides:
        config = replace(config, package=replace(config.package, **overrides))
    return Runtime.from_config(
        config, source_path=str(package), package_id=config.package.package_id
    )


def _rows(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    """Run SQL on the runtime's own connection, as positional rows.

    Preserve positional columns: reference SQL can repeat names such as ``coalesce``.
    Arrow values use the production conversion so NUMERIC stays exact and numeric.
    """
    adapter = runtime._get_adapter()  # noqa: SLF001 - the reference shares the connection
    if hasattr(adapter, "_db"):  # DuckDB
        return [tuple(row) for row in adapter._db.conn.execute(sql).fetchall()]  # noqa: SLF001
    with adapter._connection().cursor() as cursor:  # noqa: SLF001
        rows = []
        for statement in _split_sql_statements(sql):
            cursor.execute(statement)
            rows = []
            with cursor.fetch_record_batch() as reader:
                _check_postgres_result_types(reader.schema)
                for batch in reader:
                    _check_postgres_result_values(batch, reader.schema)
                    columns = [column.to_pylist() for column in batch.columns]
                    rows.extend(
                        tuple(
                            _postgres_value(value, field.type, UTC)
                            for value, field in zip(row, reader.schema, strict=True)
                        )
                        for row in zip(*columns, strict=True)
                    )
        return rows


@pytest.fixture(scope="session")
def duckdb_backend(packages: dict[str, Path]) -> Iterator[Backend]:
    runtimes = {name: _runtime(path) for name, path in packages.items()}
    try:
        for runtime in runtimes.values():
            # GLOBAL: the engine queries on cursors, which don't inherit a session setting.
            _rows(runtime, f"SET GLOBAL TimeZone = '{SESSION_ZONE}'")
        yield Backend("duckdb", runtimes, lambda sql: _rows(runtimes["utc_authored"], sql))
    finally:
        for runtime in runtimes.values():
            runtime.close()


@pytest.fixture(scope="session")
def postgres_backend(packages: dict[str, Path]) -> Iterator[Backend]:
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        pytest.skip(f"postgres: missing env {', '.join(missing)} (run `make test-postgres`)")
    schema = f"sr_correctness_{uuid.uuid4().hex[:12]}"
    options = {
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    }
    admin = _postgres(packages["utc_authored"], options)
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__}); is the server up?")
    runtimes: dict[str, Runtime] = {}
    try:
        for name, path in packages.items():
            runtimes[name] = _postgres(path, {**options, "schema": schema})
        for runtime in runtimes.values():
            _rows(runtime, f"SET TimeZone = '{SESSION_ZONE}'")
        _rows(runtimes["utc_authored"], SEED)
        yield Backend("postgres", runtimes, lambda sql: _rows(runtimes["utc_authored"], sql))
    finally:
        for runtime in runtimes.values():
            runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def _postgres(package: Path, options: dict[str, str]) -> Runtime:
    return _runtime(
        package,
        warehouse="postgres",
        default_db="",
        seed=SeedSpec(),
        connection=ConnectionSpec(kind="postgres_native", name="", options=options),
    )


VENDORS = Path(__file__).resolve().parent / "vendors"
VENDOR_NAME = "dimension.vendors_vendor_name"


class Vendors:
    """The ``vendors`` package on one backend: 236 vendors named Vendor 001 to Vendor 236 and
    one with no name, 236 products by a code (JAF-002, SES-001) and 236 shipments by a UUID."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def names(self, needle: str = "") -> list[str]:
        """Every known name holding ``needle`` (any case), in order: the reference."""
        rows = _rows(
            self.runtime,
            "SELECT vendor_name FROM vendors WHERE vendor_name IS NOT NULL ORDER BY vendor_name",
        )
        return [name for (name,) in rows if needle.lower() in name.lower()]

    def page(self, dimension_id: str = VENDOR_NAME, **arguments: Any) -> dict[str, Any]:
        return valid_values_payload(
            self.runtime, dimension_id=dimension_id, allow_live_query=True, **arguments
        )


@pytest.fixture(scope="module", params=["duckdb", "postgres"])
def vendors(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Vendors]:
    package = tmp_path_factory.mktemp("vendors") / "vendors"
    shutil.copytree(VENDORS, package)
    if request.param == "duckdb":
        runtime = _runtime(package)
        try:
            _rows(runtime, "SELECT 1")  # the fixture owns the connection
            yield Vendors(runtime)
        finally:
            runtime.close()
        return
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    if any(not os.environ.get(name, "").strip() for name in required):
        pytest.skip("postgres: SR_POSTGRES_* unset (run through with_postgres.sh)")
    schema = f"sr_vendors_{uuid.uuid4().hex[:12]}"
    options = {
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    }
    admin = _postgres(package, options)
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__})")
    runtime = _postgres(package, {**options, "schema": schema})
    try:
        _rows(runtime, (VENDORS / "data" / "seed.sql").read_text(encoding="utf-8"))
        yield Vendors(runtime)
    finally:
        runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()
