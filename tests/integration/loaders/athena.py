"""Athena fixture loader — external-table pattern.

Athena cannot INSERT rows shipped from the test host, so each fixture
table is staged as parquet on S3 (under the staging bucket the target
already requires) and exposed via Hive ``CREATE EXTERNAL TABLE …
STORED AS PARQUET``. All Hive column types are nullable, so the
fixture's NULLs round-trip unchanged. The boto3 client uses the
ambient AWS credential chain plus the connection's region — exactly
like the production adapter.

Idempotence marker: external-table catalogs have no plain
``CREATE TABLE``/``INSERT`` path, so ``_write_marker`` is a CTAS
(Athena writes the result set to the workgroup/staging location under
a per-query unique prefix). ``_marker_matches`` is inherited — plain
SELECT works.
"""

from __future__ import annotations

from urllib.parse import urlparse

from semantic_rails.db_parts.common import option_or_env

from ..fixture import FixtureTable, JaffleFixture
from . import MARKER_TABLE, FixtureLoader

# Logical fixture type -> Hive DDL type (all nullable by default).
HIVE_TYPE_MAP: dict[str, str] = {
    "integer": "bigint",
    "float": "double",
    "decimal": "decimal(18,3)",
    "string": "string",
    "date": "date",
    "timestamp": "timestamp",
    "boolean": "boolean",
}

FIXTURE_S3_PREFIX = "sr_fixture"


class AthenaFixtureLoader(FixtureLoader):
    """Stage parquet on S3, register each table as an external table."""

    # -- option resolution (the adapter's shared literal-or-*_env helper) --
    def _option_or_env(self, name: str) -> str:
        options = dict(getattr(self.adapter, "options", {}) or {})
        return option_or_env(options, name).strip()

    def _region(self) -> str:
        return self._option_or_env("region")

    def _database(self) -> str:
        options = dict(getattr(self.adapter, "options", {}) or {})
        return str(options.get("database", "") or "").strip() or "default"

    def _staging_bucket_prefix(self) -> tuple[str, str]:
        staging = self._option_or_env("s3_staging_dir")
        parsed = urlparse(staging)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError(
                f"Athena s3_staging_dir must be an s3://bucket[/prefix] URL, got {staging!r}"
            )
        prefix = parsed.path.strip("/")
        return parsed.netloc, (f"{prefix}/" if prefix else "")

    # -- loader hooks --------------------------------------------------------
    def prepare(self, fixture: JaffleFixture) -> None:
        self.execute(f"CREATE DATABASE IF NOT EXISTS {self._database()}")

    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        import boto3

        bucket, prefix = self._staging_bucket_prefix()
        table_prefix = f"{prefix}{FIXTURE_S3_PREFIX}/{fixture.fingerprint}/{table.name}/"
        client = boto3.client("s3", region_name=self._region())
        client.upload_file(
            str(fixture.parquet_path(table.name)), bucket, f"{table_prefix}part.parquet"
        )
        columns = ", ".join(
            f"{col.name} {HIVE_TYPE_MAP[col.logical_type]}" for col in table.columns
        )
        self.execute(f"DROP TABLE IF EXISTS {table.name}")
        self.execute(
            f"CREATE EXTERNAL TABLE {table.name} ({columns}) "
            f"STORED AS PARQUET LOCATION 's3://{bucket}/{table_prefix}'"
        )

    # -- marker protocol (CTAS — no plain CREATE/INSERT on Athena) -----------
    def _write_marker(self, fixture: JaffleFixture) -> None:
        self.execute(f"DROP TABLE IF EXISTS {MARKER_TABLE}")
        self.execute(
            f"CREATE TABLE {MARKER_TABLE} AS SELECT '{fixture.fingerprint}' AS fingerprint"
        )
