"""DuckLake fixture loader.

The DuckLake host is a local DuckDB process, so the fastest faithful
load path is ``CREATE TABLE … AS SELECT * FROM read_parquet(...)`` —
DuckDB infers the exact column types (NULL-friendly by construction)
from the canonical parquet fixture, so the lake holds IDENTICAL rows
and types to the reference database.

``CREATE OR REPLACE TABLE`` is supported by current DuckLake (verified
against extension build e6a3bd0a), so the load is a single atomic
statement per table; the inherited fingerprint-marker protocol makes
repeat loads no-ops anyway. Plain INSERT works too, so the inherited
marker table machinery is untouched.
"""

from __future__ import annotations

from ..fixture import FixtureTable, JaffleFixture
from . import FixtureLoader


class DuckLakeFixtureLoader(FixtureLoader):
    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        src = str(fixture.parquet_path(table.name)).replace("'", "''")
        self.execute(f"CREATE OR REPLACE TABLE {table.name} AS SELECT * FROM read_parquet('{src}')")
