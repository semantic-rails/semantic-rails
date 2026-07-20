"""Snowflake fixture loader — batched literal INSERTs with Snowflake types."""

from __future__ import annotations

from . import LiteralInsertLoader


class SnowflakeFixtureLoader(LiteralInsertLoader):
    type_map = {
        "integer": "NUMBER(38,0)",
        "float": "DOUBLE",
        "decimal": "NUMBER(18,3)",
        "string": "VARCHAR",
        "date": "DATE",
        "timestamp": "TIMESTAMP_NTZ",
        "boolean": "BOOLEAN",
    }
