"""MotherDuck fixture loader — DuckDB-native bulk load.

MotherDuck's hybrid execution reads LOCAL files referenced in SQL run
over the ``md:`` connection, so each fixture table is exactly one
``CREATE OR REPLACE TABLE <t> AS SELECT * FROM read_parquet('<path>')``
against the local parquet export. The parquet files carry the canonical
DuckDB column types (NULLs included, all columns nullable), so no DDL
type map is needed and the warehouse holds IDENTICAL rows to the DuckDB
reference.

The database itself is created lazily by the adapter
(:class:`semantic_rails.db_parts.motherduck.MotherDuckAdapter` runs
``CREATE DATABASE IF NOT EXISTS`` on first connect), so this loader
only ships table data. Idempotence comes from the inherited fingerprint
marker protocol — MotherDuck accepts plain CREATE/INSERT, so no marker
overrides are needed.
"""

from __future__ import annotations

from ..fixture import FixtureTable, JaffleFixture
from . import FixtureLoader


class MotherDuckFixtureLoader(FixtureLoader):
    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        src = str(fixture.parquet_path(table.name)).replace("'", "''")
        self.execute(f"CREATE OR REPLACE TABLE {table.name} AS SELECT * FROM read_parquet('{src}')")
