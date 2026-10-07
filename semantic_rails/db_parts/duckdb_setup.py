"""Correctness settings shared by every engine-owned DuckDB connection."""

from __future__ import annotations

import contextlib
from typing import Any

from ..errors import SemanticLayerError


def configure_duckdb_connection(connection: Any) -> Any:
    """Refuse built-in macro collisions, then merge required optimizer exclusions."""
    try:
        # Read definitions, never invoke file macros. Derive the reserved names
        # from the system catalog, including operators and built-in macros.
        functions = connection.execute(
            "SELECT database_name, function_name, function_type FROM system.main.duckdb_functions()"
        ).fetchall()
        builtins = {name.lower() for database, name, _ in functions if database == "system"}
        collisions = sorted(
            {
                name
                for database, name, kind in functions
                if database != "system"
                and kind in {"macro", "table_macro"}
                and name.lower() in builtins
            }
        )
        if collisions:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "DuckDB macros override built-in functions: " + ", ".join(collisions),
                details={"reason": "duckdb_builtin_macro_collision", "macros": collisions},
            )
        disabled = connection.execute(
            "SELECT system.main.current_setting('disabled_optimizers')"
        ).fetchone()[0]
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
