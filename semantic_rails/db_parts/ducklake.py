"""DuckLake warehouse adapter.

DuckLake is DuckDB SQL over an attached lakehouse catalog: an in-memory
DuckDB host loads the ``ducklake`` extension, ATTACHes the catalog file
(parquet data files live under ``data_path``), and ``USE``s it so
unqualified table names (``jaffle_order``) resolve against the lake.
No separate driver — it rides on the core ``duckdb`` dependency; the
extension itself is INSTALLed/LOADed by DuckDB at connect time.

Connection options (see ``DUCKLAKE_CONNECTION_OPTIONS`` in
``semantic_rails.dialects``): ``catalog_path`` / ``catalog_path_env``
(required), ``data_path`` / ``data_path_env`` (optional — DuckLake
defaults to ``<catalog>.files`` next to the catalog), and ``schema``
(optional — defaults to the catalog's ``main``). Relative paths resolve
against the repo root, not the process CWD, so ``.env`` defaults like
``data/.ducklake/catalog.ducklake`` land in a stable location.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from ..config import repo_root
from ..dialects import DUCKLAKE_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from ..sql_identifiers import quote_identifier
from .base import WarehouseAdapter
from .common import (
    DuckDbApiAdapter,
    import_driver,
    normalize_connection_options,
    option_or_env,
    require_missing_env,
)
from .duckdb_confinement import confine_duckdb, confinement_directory, require_inside
from .duckdb_setup import configure_duckdb_connection

# Alias under which the lake catalog is ATTACHed on the in-memory host.
# Invisible to compiled SQL: the adapter immediately USEs it, so table
# names stay unqualified.
_CATALOG_ALIAS = "jaffle"


def _escape_sql_string(value: str) -> str:
    return str(value).replace("'", "''")


class _NamespacedConnection:
    """DB-API facade over a duckdb connection that pins the namespace.

    duckdb's ``cursor()`` duplicates the connection, and a duplicate
    RESETS the current catalog/schema back to ``memory.main`` — a bare
    ``USE`` at connect time would silently strand every query (and the
    fixture loader!) in the in-memory catalog. Re-apply ``USE`` on each
    cursor so unqualified table names always resolve in the lake.
    """

    def __init__(self, conn: Any, use_sql: str) -> None:
        self._conn = conn
        self._use_sql = use_sql

    def cursor(self) -> Any:
        cursor = self._conn.cursor()
        try:
            cursor.execute(self._use_sql)
        except BaseException:
            cursor.close()
            raise
        return cursor

    def close(self) -> None:
        self._conn.close()


class DuckLakeAdapter(DuckDbApiAdapter):
    engine = "ducklake"
    connection_kind = "ducklake_native"
    # DuckDB has no session/statement timeout mechanism (mirrors the
    # core DuckDBAdapter); interrupt-based watchdogs are unsafe under
    # threaded ASGI hosting, so this stays truthfully False.
    supports_statement_timeout = False

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        confine_to: str | os.PathLike[str] = "",
    ) -> None:
        """``confine_to`` limits file access to that directory, which must hold the
        catalog and data paths (see :mod:`semantic_rails.db_parts.duckdb_confinement`)."""
        super().__init__()
        self.options = normalize_connection_options(
            "ducklake",
            self.connection_kind,
            options or {},
            DUCKLAKE_CONNECTION_OPTIONS,
            label="DuckLake",
        )
        self._confine_to = confinement_directory(confine_to) if confine_to else ""

    # -- path resolution ----------------------------------------------------
    def _resolve_path(self, option: str, missing_env: list[str]) -> str:
        """Resolve ``<option>`` / ``<option>_env`` to an absolute path.

        Relative paths are anchored at the repo root (NOT the process
        CWD) so the documented ``.env.example`` defaults are stable no
        matter where the server/tests are launched from.
        """
        value = option_or_env(self.options, option, missing_env)
        if not value:
            return ""
        if self._confine_to:
            return require_inside(self._confine_to, value, option=option, relative_to=repo_root())
        if not os.path.isabs(value):
            value = os.path.join(repo_root(), value)
        return os.path.abspath(value)

    def _resolve_paths(self) -> tuple[str, str]:
        missing_env: list[str] = []
        catalog_path = self._resolve_path("catalog_path", missing_env)
        data_path = self._resolve_path("data_path", missing_env)
        require_missing_env(
            missing_env,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label="DuckLake",
        )
        if not catalog_path:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "DuckLake connection requires catalog_path or catalog_path_env",
                details={"engine": self.engine, "connection_kind": self.connection_kind},
            )
        return catalog_path, data_path

    # -- DbApiAdapter hook ----------------------------------------------------
    def _create_connection(self) -> Any:
        driver = import_driver(
            "duckdb",
            extra="all",  # duckdb is a core dependency; ImportError means a broken install
            engine=self.engine,
            connection_kind=self.connection_kind,
        )
        catalog_path, data_path = self._resolve_paths()
        parent = os.path.dirname(catalog_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if data_path:
            os.makedirs(data_path, exist_ok=True)
        conn = configure_duckdb_connection(driver.connect())
        try:
            conn.execute("INSTALL ducklake")
            conn.execute("LOAD ducklake")
            attach = (
                f"ATTACH IF NOT EXISTS 'ducklake:{_escape_sql_string(catalog_path)}' "
                f"AS {_CATALOG_ALIAS}"
            )
            if data_path:
                attach += f" (DATA_PATH '{_escape_sql_string(data_path)}')"
            conn.execute(attach)
            use_target = _CATALOG_ALIAS
            schema = self.options.get("schema", "")
            if schema:
                use_target = f"{_CATALOG_ALIAS}.{quote_identifier(schema)}"
            use_sql = f"USE {use_target}"
            # Validate the namespace eagerly (catalog/schema must exist) …
            conn.execute(use_sql)
            if self._confine_to:
                # After the extension loads and the catalog attaches, which need access.
                confine_duckdb(conn, self._confine_to)
        except BaseException:
            with contextlib.suppress(Exception):  # best-effort cleanup
                conn.close()
            raise
        # … but pin it per-cursor: duckdb cursors are duplicates that
        # reset back to memory.main (see _NamespacedConnection).
        return _NamespacedConnection(conn, use_sql)


def create_adapter(
    package: Any, *, db_path: str = "", confine_to: str | os.PathLike[str] = ""
) -> WarehouseAdapter:
    """Registry entry point for the ducklake warehouse (see dialects.py)."""
    kind = package.connection.kind
    if kind != "ducklake_native":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported DuckLake connection kind '{kind}'",
            details={"engine": "ducklake", "connection_kind": str(kind)},
        )
    return DuckLakeAdapter(package.connection.options, confine_to=confine_to)
