"""DuckLake target: local DuckDB host + ducklake extension.

No service to provision — the catalog file and parquet data directory
are created on first load. Opt-in via the SR_DUCKLAKE_* env vars
(mirrored in ``.env.example``); relative paths resolve against the
repo root inside the adapter.
"""

from __future__ import annotations

from ..harness import IntegrationTarget
from ..loaders.ducklake import DuckLakeFixtureLoader

TARGET = IntegrationTarget(
    warehouse="ducklake",
    connection_kind="ducklake_native",
    connection_options={
        "catalog_path_env": "SR_DUCKLAKE_CATALOG_PATH",
        "data_path_env": "SR_DUCKLAKE_DATA_PATH",
    },
    required_env=("SR_DUCKLAKE_CATALOG_PATH", "SR_DUCKLAKE_DATA_PATH"),
    make_loader=DuckLakeFixtureLoader,
    notes=(
        "Local lakehouse — duckdb core + ducklake extension (network fetch on "
        "first INSTALL). Set SR_DUCKLAKE_CATALOG_PATH / SR_DUCKLAKE_DATA_PATH "
        "to opt in; see .env.example."
    ),
)
