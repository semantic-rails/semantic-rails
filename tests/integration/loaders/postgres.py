"""Postgres fixture loader.

Plain :class:`LiteralInsertLoader` with the Postgres DDL type map —
Postgres accepts standard CREATE TABLE + multi-row INSERT, and columns
are nullable by default, so the fixture's NULL-bearing rows load as-is.
Idempotence comes from the inherited fingerprint marker table.
"""

from __future__ import annotations

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
