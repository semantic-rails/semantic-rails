"""Warehouse adapters and the DuckDB seed bootstrap.

Exposes :class:`WarehouseAdapter` (the pluggable warehouse contract),
:func:`create_warehouse_adapter` (returns a DuckDB / Snowflake CLI /
Snowflake native instance based on package config), :class:`Database`
(the local DuckDB convenience wrapper), and :func:`seed_db` /
:func:`load_csv_dir_to_duckdb` for the local quickstart's seed pipeline.
:func:`build_seed_database` builds a seed beside its target and records its
provenance (see :mod:`semantic_rails.seed_provenance`) for operator inspection.
Runtime bootstrap never uses provenance to authorize replacing an existing
file. Adapters are
the only place that touches a real warehouse driver — the rest of the
runtime talks to the abstract interface.

The Snowflake CLI / native adapters and their connection-option
helpers live in :mod:`semantic_rails.db_parts.snowflake` to keep this
module under the 500-LOC ceiling. Every name previously exported here
is still re-exported so external callers do not need to change.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sqlite3
import subprocess  # noqa: F401 — re-exported for tests that monkeypatch semantic_rails.db.subprocess
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None  # type: ignore[assignment]  # optional dependency fallback

from .db_parts.base import (
    ConnectionCredentialProvider,
    QueryRows,
    WarehouseAdapter,
    _clip_rows,
    _limit_max_rows,
    _limit_timeout_milliseconds,
)
from .db_parts.snowflake import (
    SnowflakeCliAdapter,
    SnowflakeNativeAdapter,
    build_snowflake_cli_command,
)
from .dialects import (
    supported_warehouses,
    warehouse_connector,
)
from .errors import SemanticLayerError, query_execution_error
from .schema import PackageMeta
from .seed_provenance import record_seed_provenance
from .sql_preparation import PreparedQuery

__all__ = [
    "Database",
    "PreparedQuery",
    "DuckDBAdapter",
    "ConnectionCredentialProvider",
    "SnowflakeCliAdapter",
    "SnowflakeNativeAdapter",
    "WarehouseAdapter",
    "build_snowflake_cli_command",
    "build_seed_database",
    "create_duckdb_adapter",
    "create_warehouse_adapter",
    "load_csv_dir_to_duckdb",
    "seed_db",
]


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    for ch in sql:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == ";" and not in_single and not in_double:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


@dataclass
class Database:
    conn: Any
    engine: str

    @classmethod
    def connect(cls, db_path: str, *, engine: str = "duckdb", read_only: bool = False) -> Database:
        if engine == "duckdb":
            if duckdb is None:
                raise RuntimeError(
                    "duckdb is not installed. Add it to your environment dependencies."
                )
            return cls(conn=duckdb.connect(db_path, read_only=read_only), engine=engine)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return cls(conn=conn, engine="sqlite")

    @classmethod
    def connect_in_memory(cls) -> Database:
        if duckdb is None:
            raise RuntimeError("duckdb is not installed. Add it to your environment dependencies.")
        return cls(conn=duckdb.connect(":memory:"), engine="duckdb")

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> None:
        cur = self.conn.cursor()
        cur.execute(sql, list(params or []))
        if hasattr(self.conn, "commit"):
            self.conn.commit()

    def execute_script(self, sql: str) -> None:
        if self.engine == "sqlite":
            self.conn.executescript(sql)
        else:
            for statement in _split_sql_statements(sql):
                self.conn.execute(statement)
        if hasattr(self.conn, "commit"):
            self.conn.commit()

    def query(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(sql, list(params or []))
        fetched = cur.fetchmany(max_rows + 1) if max_rows is not None else cur.fetchall()
        truncated = max_rows is not None and len(fetched) > max_rows
        if truncated:
            fetched = fetched[:max_rows]
        return QueryRows(
            [_row_to_dict(cur, row) for row in fetched],
            truncated=truncated,
        )

    def close(self) -> None:
        self.conn.close()


class DuckDBAdapter(WarehouseAdapter):
    engine = "duckdb"
    supports_statement_timeout = True

    def __init__(self, db_path: str):
        self._db = Database.connect(db_path, read_only=True)

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        timeout_ms = _limit_timeout_milliseconds(limits)
        finished = threading.Event()
        watchdog: threading.Timer | None = None
        if timeout_ms > 0:

            def interrupt_if_running() -> None:
                if not finished.is_set():
                    self._db.conn.interrupt()

            watchdog = threading.Timer(timeout_ms / 1_000.0, interrupt_if_running)
            watchdog.daemon = True
            watchdog.start()
        try:
            rows = self._db.query(sql, max_rows=_limit_max_rows(limits))
            return _clip_rows(rows, limits)
        except SemanticLayerError:
            raise
        except Exception as exc:
            raise query_execution_error({"engine": self.engine, "sql_redacted": True}) from exc
        finally:
            finished.set()
            if watchdog is not None:
                watchdog.cancel()
                # Ensure a callback that won the cancellation race has
                # finished before the shared connection can run another query.
                watchdog.join()

    def close(self) -> None:
        self._db.close()


def create_duckdb_adapter(package: PackageMeta, *, db_path: str = "") -> WarehouseAdapter:
    """Registry entry point for the duckdb warehouse (see dialects.py)."""
    if not db_path:
        raise SemanticLayerError("INVALID_CONFIG", "DuckDB adapter requires a database path")
    return DuckDBAdapter(db_path)


def create_warehouse_adapter(package: PackageMeta, *, db_path: str = "") -> WarehouseAdapter:
    """Build the execution adapter for a package's warehouse.

    Fully registry-driven: each :class:`~semantic_rails.dialects.WarehouseConnectorSpec`
    names its adapter factory as a ``"module:callable"`` entry point,
    resolved lazily here so optional drivers are only imported for the
    warehouse actually in use. Adding a new warehouse requires no edit
    to this function — see docs/ADDING_A_DIALECT.md.
    """
    warehouse = str(package.warehouse or "duckdb").strip().lower() or "duckdb"
    connector = warehouse_connector(warehouse)
    if connector is None:
        supported = ", ".join(supported_warehouses())
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported package warehouse '{warehouse}' (supported: {supported})",
        )
    if not connector.adapter:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Warehouse '{warehouse}' does not have an execution adapter"
        )
    module_name, _, attr = connector.adapter.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Warehouse '{warehouse}' adapter entry point '{connector.adapter}' could not be resolved: {exc}",
        ) from exc
    return factory(package, db_path=db_path)


def _remove_quietly(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _atomic_seed_target(db_path: str) -> str:
    # Seed into a sibling temp file, then publish it. Concurrent seeders
    # (e.g. pytest-xdist workers, parallel CLI runs or threads racing to create
    # the same default_db) each build a complete file of their own and never
    # contend for DuckDB's single-writer lock on the shared path.
    return f"{db_path}.seed.{os.getpid()}.{uuid.uuid4().hex}.tmp"


def _duckdb_string_list(values: Iterable[str]) -> str:
    escaped = [str(value).replace("'", "''") for value in values]
    return "[" + ", ".join(f"'{value}'" for value in escaped) + "]"


def _build_sql_seed(db: Database, seed_sql_path: str) -> None:
    with open(seed_sql_path, encoding="utf-8") as f:
        db.execute_script(f.read())


def _build_csv_seed(
    db: Database, csv_dir: str, post_sql_path: str, null_strings: Iterable[str] | None
) -> None:
    null_values = list(null_strings or [""])
    null_clause = f", NULLSTR={_duckdb_string_list(null_values)}" if null_values else ""
    for filename in sorted(name for name in os.listdir(csv_dir) if name.endswith(".csv")):
        table_name = os.path.splitext(filename)[0]
        src = os.path.join(csv_dir, filename).replace("'", "''")
        try:
            db.execute(
                f"""
                CREATE OR REPLACE TABLE {table_name} AS
                SELECT *
                FROM read_csv_auto('{src}', HEADER=TRUE{null_clause});
                """.strip()
            )
        except Exception as exc:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"CSV seed load failed for file '{filename}' into table '{table_name}': {exc}",
                details={"csv_dir": csv_dir, "file": filename, "table": table_name},
            ) from exc
    if post_sql_path:
        try:
            with open(post_sql_path, encoding="utf-8") as f:
                db.execute_script(f.read())
        except Exception as exc:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"CSV seed post_sql failed for '{post_sql_path}': {exc}",
                details={"csv_dir": csv_dir, "post_sql": post_sql_path},
            ) from exc


def build_seed_database(
    db_path: str,
    *,
    kind: str,
    source: str,
    post_sql: str = "",
    null_strings: Iterable[str] | None = None,
    package_id: str = "",
) -> str:
    """Build a seed into a fresh file beside ``db_path`` and return that file's path.

    ``kind`` is ``sql_script`` (``source`` is a SQL file) or ``csv_dir_duckdb``
    (``source`` is a directory of CSVs, plus optional ``post_sql``). With
    ``package_id`` the file records that this package's seed built it. The
    runtime callers publish it without replacing an existing target via
    :func:`semantic_rails.seed_provenance.publish_seed_database`; explicit
    seeding helpers retain their operator-invoked replace behavior.
    """
    if kind not in {"sql_script", "csv_dir_duckdb"}:
        raise SemanticLayerError("INVALID_CONFIG", f"Unsupported seed kind '{kind}'")
    if duckdb is None:
        raise RuntimeError("duckdb is not installed. Add it to your environment dependencies.")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    tmp_path = _atomic_seed_target(db_path)
    _remove_quietly(tmp_path)
    try:
        db = Database.connect(tmp_path)
        try:
            if kind == "sql_script":
                _build_sql_seed(db, source)
            else:
                _build_csv_seed(db, source, post_sql, null_strings)
        finally:
            db.close()
        if package_id:
            record_seed_provenance(tmp_path, package_id)
    except BaseException:
        _remove_quietly(tmp_path)
        raise
    return tmp_path


def _build_and_replace(db_path: str, **build: Any) -> None:
    tmp_path = build_seed_database(db_path, **build)
    try:
        # Explicit seeding helpers keep their existing replace semantics; runtime
        # bootstrap uses publish_seed_database directly, which never overwrites.
        # A destination WAL may contain committed changes absent from the main
        # file. Only its owner can safely close/checkpoint it before replacement.
        if os.path.lexists(f"{db_path}.wal"):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"Cannot replace '{db_path}' while its DuckDB WAL exists; "
                "close and checkpoint the database first",
                details={"reason": "seed_target_wal_present", "db_path": db_path},
            )
        os.replace(tmp_path, db_path)
    finally:
        _remove_quietly(tmp_path)


def seed_db(db_path: str, seed_sql_path: str, *, package_id: str = "") -> None:
    """Build from SQL and replace ``db_path`` if no destination WAL exists."""
    _build_and_replace(db_path, kind="sql_script", source=seed_sql_path, package_id=package_id)


def load_csv_dir_to_duckdb(
    db_path: str,
    csv_dir: str,
    post_sql_path: str = "",
    null_strings: Iterable[str] | None = None,
    *,
    package_id: str = "",
) -> None:
    """Build from CSVs (plus optional SQL) and replace if no destination WAL exists."""
    _build_and_replace(
        db_path,
        kind="csv_dir_duckdb",
        source=csv_dir,
        post_sql=post_sql_path,
        null_strings=null_strings,
        package_id=package_id,
    )
