"""Postgres fixture loader.

Typed tables populated through ADBC's Arrow ingestion on the production
connection. Idempotence comes from the inherited fingerprint marker table.
"""

from __future__ import annotations

from ..fixture import FixtureTable, JaffleFixture
from . import LiteralInsertLoader


class PostgresFixtureLoader(LiteralInsertLoader):
    type_map = {
        "integer": "BIGINT",
        "float": "DOUBLE PRECISION",
        "decimal": "DECIMAL(18,3)",
        "string": "TEXT",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "boolean": "BOOLEAN",
    }

    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        import pyarrow.parquet as parquet

        self.execute(f"DROP TABLE IF EXISTS {table.name}")
        self.execute(self.create_table_sql(table))
        with self.adapter._lock, self.adapter._connection().cursor() as cursor:
            for batch in parquet.ParquetFile(fixture.parquet_path(table.name)).iter_batches(
                batch_size=self.batch_rows
            ):
                cursor.adbc_ingest(
                    table.name,
                    batch,
                    mode="append",
                    db_schema_name=self.adapter.options.get("schema") or None,
                )
