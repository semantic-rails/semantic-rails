"""ClickHouse fixture loader.

DDL goes through the production adapter (``adapter.query``) so the
loader exercises the runtime's connection path; row loading reaches
into the adapter for its underlying ``clickhouse_connect`` client and
uses ``client.insert`` — the driver's canonical bulk path (fine in
test code).

Every column is ``Nullable(...)``: the fixture data contains NULLs and
ClickHouse types are non-nullable by default.

The marker DDL knobs are overridden because ClickHouse CREATE TABLE
requires an engine clause (``TinyLog`` is enough for the one-row
fingerprint marker) and spells the string type ``String``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..fixture import FixtureTable, JaffleFixture
from . import FixtureLoader, iter_table_batches

# Logical fixture type -> ClickHouse column type (wrapped in Nullable).
CLICKHOUSE_TYPE_MAP: dict[str, str] = {
    "integer": "Int64",
    "float": "Float64",
    "decimal": "Decimal(18,3)",
    "string": "String",
    "date": "Date32",
    "timestamp": "DateTime64(3)",
    "boolean": "Bool",
}


def _utc_aware(value: Any) -> Any:
    """Attach UTC to naive datetimes before insert.

    clickhouse_connect converts naive datetimes to epoch seconds via
    ``datetime.timestamp()``, which interprets them in the LOCAL
    timezone — shifting every fixture timestamp by the local UTC
    offset (verified live: a naive ``2017-02-01 00:00`` round-tripped
    as ``05:00`` from an EST client against a UTC server). Marking the
    values UTC-aware keeps the stored wall clock identical to the
    parquet fixture on a UTC server (the docker-compose default).
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class ClickHouseFixtureLoader(FixtureLoader):
    def create_table_sql(self, table: FixtureTable) -> str:
        columns = ", ".join(
            f"{col.name} Nullable({CLICKHOUSE_TYPE_MAP[col.logical_type]})" for col in table.columns
        )
        return f"CREATE TABLE {table.name} ({columns}) ENGINE = MergeTree ORDER BY tuple()"

    # ClickHouse CREATE TABLE requires an engine clause; TinyLog is
    # enough for the one-row fingerprint marker.
    marker_fingerprint_type = "String"
    marker_table_suffix = " ENGINE = TinyLog"

    def load_table(self, fixture: JaffleFixture, table: FixtureTable) -> None:
        self.execute(f"DROP TABLE IF EXISTS {table.name}")
        self.execute(self.create_table_sql(table))
        client = self.adapter._client_handle()
        column_names = [col.name for col in table.columns]
        for batch in iter_table_batches(fixture, table):
            rows = [tuple(_utc_aware(value) for value in row) for row in batch]
            client.insert(table.name, rows, column_names=column_names)
