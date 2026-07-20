"""BigQuery fixture loader — parquet load jobs (Sandbox-safe).

BigQuery Sandbox blocks INSERT DML, so this loader never goes through
``LiteralInsertLoader``. Each table loads via
``client.load_table_from_file`` with ``WRITE_TRUNCATE`` and an explicit
schema derived from the fixture's logical types (every column NULLABLE
— the fixture data contains NULLs). The idempotence marker is written
with CTAS (``CREATE OR REPLACE TABLE … AS SELECT``), a query-job DDL
form the sandbox allows, instead of the inherited CREATE+INSERT pair.

Statements and load jobs reuse the production adapter's client, so the
loader exercises the same connection path the runtime uses.
"""

from __future__ import annotations

from typing import Any

from ..fixture import FixtureTable, JaffleFixture
from . import MARKER_TABLE, FixtureLoader

# Logical type -> BigQuery type (load-job schema).
BIGQUERY_TYPE_MAP: dict[str, str] = {
    "integer": "INT64",
    "float": "FLOAT64",
    "decimal": "NUMERIC",
    "string": "STRING",
    "date": "DATE",
    "timestamp": "DATETIME",
    "boolean": "BOOL",
}


class BigQueryFixtureLoader(FixtureLoader):
    type_map: dict[str, str] = BIGQUERY_TYPE_MAP

    def _bigquery(self) -> Any:
        import google.cloud.bigquery as bigquery

        return bigquery

    def prepare(self, fixture: JaffleFixture) -> None:
        dataset_id = self.adapter.default_dataset_id()
        if dataset_id:
            self.adapter.client().create_dataset(dataset_id, exists_ok=True)

    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        bigquery = self._bigquery()
        client = self.adapter.client()
        schema = [
            bigquery.SchemaField(col.name, self.type_map[col.logical_type], mode="NULLABLE")
            for col in table.columns
        ]
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=schema,
        )
        destination = f"{self.adapter.default_dataset_id()}.{table.name}"
        with open(fixture.parquet_path(table.name), "rb") as handle:
            job = client.load_table_from_file(handle, destination, job_config=job_config)
        job.result()

    # -- marker protocol (sandbox-safe override) ------------------------
    def _write_marker(self, fixture: JaffleFixture) -> None:
        # The inherited CREATE TABLE + INSERT pair is DML the sandbox
        # blocks; CTAS is an allowed query job.
        fingerprint = str(fixture.fingerprint).replace("'", "''")
        self.execute(
            f"CREATE OR REPLACE TABLE {MARKER_TABLE} AS SELECT '{fingerprint}' AS fingerprint"
        )
