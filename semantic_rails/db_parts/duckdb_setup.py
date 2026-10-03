"""Correctness settings shared by every engine-owned DuckDB connection."""

from __future__ import annotations

import contextlib
from typing import Any


def configure_duckdb_connection(connection: Any) -> Any:
    """Merge required optimizer exclusions before any warehouse SQL runs."""
    try:
        disabled = connection.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
        optimizers = [name.strip() for name in disabled.split(",") if name.strip()]
        # DuckDB 1.5.6 can reuse the wrong aggregate over CASE-filtered views.
        # Remove this exclusion once a fixed DuckDB version is the dependency floor.
        if "common_subplan" not in optimizers:
            connection.execute(
                "SET disabled_optimizers = ?", [",".join([*optimizers, "common_subplan"])]
            )
        return connection
    except BaseException:
        with contextlib.suppress(Exception):
            connection.close()
        raise
