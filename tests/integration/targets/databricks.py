"""Databricks SQL integration target.

Requires a running SQL Warehouse and a personal access token; see
``.env.example`` (``SR_DATABRICKS_*``). The catalog/schema namespace is
read at import time (defaults stay unset, in which case the warehouse's
session defaults apply) and passed as connection options so unqualified
fixture tables resolve.
"""

from __future__ import annotations

import os

from ..harness import IntegrationTarget
from ..loaders.databricks import DatabricksFixtureLoader

_CONNECTION_OPTIONS: dict[str, str] = {
    "host_env": "SR_DATABRICKS_HOST",
    "http_path_env": "SR_DATABRICKS_HTTP_PATH",
    "token_env": "SR_DATABRICKS_TOKEN",
}
# Optional namespace — import-time defaults mirror .env.example.
_catalog = os.environ.get("SR_DATABRICKS_CATALOG", "").strip()
_schema = os.environ.get("SR_DATABRICKS_SCHEMA", "").strip()
if _catalog:
    _CONNECTION_OPTIONS["catalog"] = _catalog
if _schema:
    _CONNECTION_OPTIONS["schema"] = _schema

TARGET = IntegrationTarget(
    warehouse="databricks",
    connection_kind="databricks_native",
    connection_options=_CONNECTION_OPTIONS,
    required_env=(
        "SR_DATABRICKS_HOST",
        "SR_DATABRICKS_HTTP_PATH",
        "SR_DATABRICKS_TOKEN",
    ),
    make_loader=DatabricksFixtureLoader,
    notes="Databricks SQL Warehouse over databricks-sql-connector (PAT auth).",
)
