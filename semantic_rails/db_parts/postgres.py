"""Postgres warehouse adapter (``postgres_native``, psycopg 3).

Registry entry point for the postgres warehouse (see
``semantic_rails.dialects``): :func:`create_adapter` builds a
:class:`PostgresAdapter` from ``package.connection.options``.

Conventions (shared machinery in :mod:`semantic_rails.db_parts.common`):

- Secrets only via env-var indirection (``password_env``) or file paths
  (``password_file``); literal secret keys are rejected at
  normalization time.
- Errors are redacted — engine, connection kind, option KEYS,
  ``sql_redacted`` only; driver text stays private.
- psycopg is an optional extra (``semantic-rails[postgres]``), imported
  lazily so a missing driver maps to ``MISSING_DEPENDENCY``.
- The connection defaults its namespace from ``schema`` via
  ``search_path`` so unqualified table names (``jaffle_order``)
  resolve without per-query qualification.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..dialects import POSTGRES_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter
from .common import (
    DbApiAdapter,
    import_driver,
    int_option,
    normalize_connection_options,
    option_or_env,
    require_missing_env,
    secret_value,
)

_LABEL = "Postgres"
_DEFAULT_PORT = 5432
# Names a server may report for UTC (the Docker image says Etc/UTC), all one zone.
_UTC_NAMES = frozenset({"utc", "etc/utc", "uct", "etc/uct", "universal", "etc/universal"})
_UTC_NAMES |= {"zulu", "etc/zulu", "gmt", "etc/gmt", "greenwich", "etc/greenwich"}


def _same_zone(left: str, right: str) -> bool:
    left, right = left.casefold(), right.casefold()
    return left == right or (left in _UTC_NAMES and right in _UTC_NAMES)


class _DdlTolerantCursor:
    """psycopg 3 raises ``ProgrammingError`` when fetching from a
    statement that returns no result set (DDL, INSERT, SET) — most
    DB-API peers return ``[]`` instead. The shared
    :meth:`DbApiAdapter.query` always fetches, so map the no-result
    case (``description is None``) to an empty row list."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def fetchall(self) -> list[Any]:
        if self._cursor.description is None:
            return []
        return self._cursor.fetchall()


class _DdlTolerantConnection:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def cursor(self) -> _DdlTolerantCursor:
        return _DdlTolerantCursor(self._conn.cursor())


class PostgresAdapter(DbApiAdapter):
    engine = "postgres"
    connection_kind = "postgres_native"
    # Honors limits.statement_timeout_ms natively via the session-level
    # `SET statement_timeout` (milliseconds), reset after each query.
    supports_statement_timeout = True

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.options = normalize_connection_options(
            "postgres",
            self.connection_kind,
            options or {},
            POSTGRES_CONNECTION_OPTIONS,
            label=_LABEL,
        )

    def _int_option(self, name: str, default: int) -> int:
        return int_option(
            self.options,
            name,
            default,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label=_LABEL,
        )

    def _connect_kwargs(self) -> dict[str, Any]:
        missing_env: list[str] = []
        host = option_or_env(self.options, "host", missing_env)
        user = option_or_env(self.options, "user", missing_env)
        password = secret_value(
            "password",
            self.options.get("password_env", ""),
            self.options.get("password_file", ""),
            missing_env,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label=_LABEL,
        )
        require_missing_env(
            missing_env, engine=self.engine, connection_kind=self.connection_kind, label=_LABEL
        )
        kwargs: dict[str, Any] = {
            "port": self._int_option("port", _DEFAULT_PORT),
            "autocommit": True,
        }
        if host:
            kwargs["host"] = host
        if user:
            kwargs["user"] = user
        if password:
            kwargs["password"] = password
        if self.options.get("database"):
            kwargs["dbname"] = self.options["database"]
        if self.options.get("sslmode"):
            kwargs["sslmode"] = self.options["sslmode"]
        # Session startup options: default the namespace so unqualified
        # table names resolve, and apply a configured default timeout.
        startup_options: list[str] = []
        if self.options.get("schema"):
            startup_options.append(f"-c search_path={self.options['schema']}")
        timeout_seconds = self._int_option("statement_timeout_seconds", 0)
        if timeout_seconds > 0:
            startup_options.append(f"-c statement_timeout={timeout_seconds * 1000}")
        if startup_options:
            kwargs["options"] = " ".join(startup_options)
        return kwargs

    def _create_connection(self) -> Any:
        driver = import_driver(
            "psycopg",
            extra="postgres",
            engine=self.engine,
            connection_kind=self.connection_kind,
        )
        return _DdlTolerantConnection(driver.connect(**self._connect_kwargs()))

    def _apply_statement_timeout(self, cursor: Any, timeout_seconds: int) -> None:
        cursor.execute(f"SET statement_timeout = {int(timeout_seconds) * 1000}")

    def _reset_statement_timeout(self, cursor: Any) -> None:
        cursor.execute("RESET statement_timeout")

    @contextmanager
    def _time_zone_scope(self, cursor: Any, zone: str) -> Iterator[None]:
        """Run the statement with ``SET LOCAL TimeZone``, which ends with its transaction.

        Outside a transaction, the scope is a transaction of its own. Inside one the
        connection's owner opened, ``SET LOCAL`` would outlast this statement, so the
        owner's zone is put back before its transaction goes on.
        """
        info = self._connection().info
        current = info.parameter_status("TimeZone")
        if not current:  # a proxy that doesn't report it: ask the server
            cursor.execute("SELECT current_setting('TimeZone')")
            current = cursor.fetchone()[0]
        if _same_zone(current, zone):
            yield
            return
        idle = getattr(info.transaction_status, "name", "") == "IDLE"
        with self._connection().transaction():
            cursor.execute("SELECT set_config('TimeZone', %s, true)", (zone,))
            yield
            if not idle:
                cursor.execute("SELECT set_config('TimeZone', %s, true)", (current,))


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    """Registry entry point for the postgres warehouse (see dialects.py)."""
    kind = str(package.connection.kind or "").strip()
    if kind != "postgres_native":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported Postgres connection kind '{kind}'",
            details={"engine": "postgres", "connection_kind": kind},
        )
    return PostgresAdapter(package.connection.options)
