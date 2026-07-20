"""Fixture loaders — load the canonical JaffleShop parquet fixture into
a warehouse so every dialect queries IDENTICAL rows.

Shared machinery lives here; each warehouse adds one small module
``loaders/<warehouse>.py`` exposing a :class:`FixtureLoader` subclass
(usually just :class:`LiteralInsertLoader` with a type map, or an
override of :meth:`FixtureLoader.load_table` for bulk paths like
BigQuery load jobs or Athena external tables).

Idempotence: loaders record the fixture fingerprint in a one-row marker
table (``sr_fixture_meta``). A matching fingerprint skips the load, so
repeated test runs against cloud warehouses cost one round-trip.

All statements go through the production adapter (``adapter.query``),
so the loader exercises the same connection path the runtime uses and
never duplicates driver/connection logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..fixture import FixtureTable, JaffleFixture

MARKER_TABLE = "sr_fixture_meta"
DEFAULT_BATCH_ROWS = 5000

# Default logical-type -> DDL type map (ANSI-ish). Loaders override
# entries per warehouse (e.g. ClickHouse `String`, BigQuery `FLOAT64`).
DEFAULT_TYPE_MAP: dict[str, str] = {
    "integer": "BIGINT",
    "float": "DOUBLE PRECISION",
    "decimal": "DECIMAL(18,3)",
    "string": "VARCHAR",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "boolean": "BOOLEAN",
}


def iter_table_batches(
    fixture: JaffleFixture, table: FixtureTable, batch_rows: int = DEFAULT_BATCH_ROWS
) -> Iterator[list[tuple[Any, ...]]]:
    """Yield row batches from a fixture table's parquet file."""
    import duckdb

    src = str(fixture.parquet_path(table.name)).replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        cur = con.execute(f"SELECT * FROM read_parquet('{src}')")
        while True:
            batch = cur.fetchmany(batch_rows)
            if not batch:
                return
            yield batch
    finally:
        con.close()


class FixtureLoader(ABC):
    """Loads the canonical fixture into one warehouse, idempotently."""

    # Marker-table DDL knobs: warehouses that accept plain CREATE+INSERT
    # but spell the string type differently (Databricks STRING,
    # ClickHouse String) or need a table suffix (ClickHouse ENGINE
    # clause) override these instead of re-implementing _write_marker.
    # Catalogs without plain CREATE/INSERT (Athena, BigQuery Sandbox)
    # override _write_marker itself with a CTAS form.
    marker_fingerprint_type: str = "VARCHAR"
    marker_table_suffix: str = ""

    def __init__(self, adapter: Any):
        self.adapter = adapter

    # -- public entry point -------------------------------------------------
    def ensure_loaded(self, fixture: JaffleFixture) -> None:
        if self._marker_matches(fixture):
            return
        self.prepare(fixture)
        for table in fixture.tables:
            self.load_table(fixture, table)
        self._write_marker(fixture)

    # -- per-warehouse hooks -------------------------------------------------
    def prepare(self, fixture: JaffleFixture) -> None:
        """Optional pre-load hook (create scratch schema/dataset, …)."""
        return None

    @abstractmethod
    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        """Create (or replace) one table and load its parquet rows."""

    # -- marker protocol -----------------------------------------------------
    def _marker_matches(self, fixture: JaffleFixture) -> bool:
        try:
            rows = self.adapter.query(f"SELECT fingerprint FROM {MARKER_TABLE}")
        except Exception:  # noqa: BLE001 - marker table absent on first load
            return False
        values = {str(value) for row in rows for value in row.values()}
        return fixture.fingerprint in values

    def _write_marker(self, fixture: JaffleFixture) -> None:
        self.execute(f"DROP TABLE IF EXISTS {MARKER_TABLE}")
        self.execute(
            f"CREATE TABLE {MARKER_TABLE} "
            f"(fingerprint {self.marker_fingerprint_type}){self.marker_table_suffix}"
        )
        self.execute(f"INSERT INTO {MARKER_TABLE} VALUES ('{fixture.fingerprint}')")

    # -- helpers --------------------------------------------------------------
    def execute(self, sql: str) -> None:
        self.adapter.query(sql)


class LiteralInsertLoader(FixtureLoader):
    """Generic loader: typed CREATE TABLE + batched multi-row INSERTs of
    SQL literals. Works on any adapter whose warehouse accepts standard
    INSERT DML; warehouses with a faster bulk path override
    :meth:`load_table` instead.
    """

    type_map: dict[str, str] = DEFAULT_TYPE_MAP
    batch_rows = DEFAULT_BATCH_ROWS

    def create_table_sql(self, table: FixtureTable) -> str:
        columns = ", ".join(
            f"{col.name} {self.type_map[col.logical_type]}" for col in table.columns
        )
        return f"CREATE TABLE {table.name} ({columns})"

    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        self.execute(f"DROP TABLE IF EXISTS {table.name}")
        self.execute(self.create_table_sql(table))
        column_list = ", ".join(col.name for col in table.columns)
        for batch in iter_table_batches(fixture, table, self.batch_rows):
            values = ",\n".join(
                "(" + ", ".join(self.sql_literal(value) for value in row) + ")" for row in batch
            )
            self.execute(f"INSERT INTO {table.name} ({column_list}) VALUES {values}")

    def sql_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        if isinstance(value, datetime):
            return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
        if isinstance(value, date):
            return f"DATE '{value.isoformat()}'"
        text = str(value).replace("'", "''")
        return f"'{text}'"
