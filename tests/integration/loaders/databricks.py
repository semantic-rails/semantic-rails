"""Databricks fixture loader.

Standard :class:`LiteralInsertLoader` path: typed ``CREATE TABLE`` +
batched multi-row ``INSERT`` of SQL literals over the production
adapter. Delta columns are nullable by default, so the fixture's NULLs
load as-is. The only override is the fingerprint-marker DDL: Databricks
SQL rejects bare ``VARCHAR`` (it requires a length), so the marker
column is ``STRING``.

Batches are kept small-ish (~2000 rows) because each batch is one HTTP
round-trip with an inline VALUES list; the inherited fingerprint marker
makes the whole load one-time per fixture version.
"""

from __future__ import annotations

from . import LiteralInsertLoader


class DatabricksFixtureLoader(LiteralInsertLoader):
    type_map = {
        "integer": "BIGINT",
        "float": "DOUBLE",
        "decimal": "DECIMAL(18,3)",
        "string": "STRING",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "boolean": "BOOLEAN",
    }
    batch_rows = 2000
    # Bare VARCHAR (no length) is invalid on Databricks — use STRING.
    marker_fingerprint_type = "STRING"
