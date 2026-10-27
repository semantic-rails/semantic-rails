"""The one way the engine opens a DuckDB connection, and the settings every one gets."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

import duckdb

from ..errors import SemanticLayerError


def connect_duckdb(
    database: str = ":memory:",
    *,
    read_only: bool = False,
    config: Mapping[str, Any] | None = None,
) -> Any:
    """Open a DuckDB connection with the engine's correctness settings. Every connection the
    engine opens (local files, MotherDuck, DuckLake, introspection, seeds) comes from here, so
    none can miss a setting; a cursor of it shares them."""
    return configure_duckdb_connection(
        duckdb.connect(database, read_only=read_only, config=dict(config or {}))
    )


def configure_duckdb_connection(connection: Any) -> Any:
    """Refuse built-in macro collisions, merge required optimizer exclusions, and draw no
    progress bar."""
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
        # DuckDB's Python client draws a progress bar on stdout for any statement slower than
        # 2 s: into a server's log stream, or an MCP stdio server's JSON-RPC stream. A cursor
        # of the connection keeps the setting, and a confined instance allows it
        # (duckdb_confinement); an instance locked any other way keeps DuckDB's default.
        locked, allowed = connection.execute(
            "SELECT system.main.current_setting('lock_configuration'), "
            "system.main.current_setting('allowed_configs')"
        ).fetchone()
        if not locked or "enable_progress_bar" in (allowed or ()):
            connection.execute("SET enable_progress_bar = false")
        return connection
    except BaseException:
        with contextlib.suppress(Exception):
            connection.close()
        raise
