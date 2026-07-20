"""Postgres warehouse adapter (``postgres_native``, psycopg 3).

Registry entry point for the postgres warehouse (see
``semantic_rails.dialects``): :func:`create_adapter` builds a
:class:`PostgresAdapter` from ``package.connection.options``.

Conventions (shared machinery in :mod:`semantic_rails.db_parts.common`):

- Secrets only via env-var indirection (``password_env``) or file paths
  (``password_file``); literal secret keys are rejected at
  normalization time.
- Errors are redacted — engine, connection kind, option KEYS,
  ``sql_redacted``, and bounded driver text only.
- psycopg is an optional extra (``semantic-rails[postgres]``), imported
  lazily so a missing driver maps to ``MISSING_DEPENDENCY``.
- The connection defaults its namespace from ``schema`` via
  ``search_path`` so unqualified table names (``jaffle_order``)
  resolve without per-query qualification.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..dialects import POSTGRES_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter
from .common import (
    DbApiAdapter,
    float_nullif_divisions,
    import_driver,
    int_option,
    normalize_connection_options,
    option_or_env,
    require_missing_env,
    secret_value,
)

_LABEL = "Postgres"
_DEFAULT_PORT = 5432


# ---------------------------------------------------------------------------
# PostgreSQL compatibility pass — two semantics-preserving rewrites of the
# compiler's rendered SQL, applied just before execution. Both compensate
# for hard PostgreSQL limits the portable SQL AST cannot express: the
# identifier shortening below, plus the shared literal-aware
# `common.float_nullif_divisions` pass with PG's DOUBLE PRECISION cast.
# The pass never touches string literals.
# ---------------------------------------------------------------------------

# NAMEDATALEN-1: PostgreSQL silently truncates longer identifiers, which
# makes two long compiler-generated CTE names that share a 63-byte prefix
# collide ("WITH query name ... specified more than once").
_MAX_IDENTIFIER_BYTES = 63
_IDENT_HASH_CHARS = 10

# Tokens: single-quoted literal | quoted identifier | bare identifier.
_SQL_TOKEN_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")+\"|[A-Za-z_][A-Za-z0-9_]*")


def _shortened_identifier(name: str) -> str:
    """Deterministically shorten a too-long identifier, keeping a readable
    prefix and a content hash so distinct names stay distinct and every
    reference to the same name rewrites identically."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_IDENT_HASH_CHARS]
    keep = _MAX_IDENTIFIER_BYTES - _IDENT_HASH_CHARS - 1
    return f"{name[:keep]}_{digest}"


def _shorten_long_identifiers(sql: str) -> str:
    """Rewrite identifiers longer than 63 bytes (quoted or bare) to a
    hash-suffixed 63-byte form. PostgreSQL truncates ALL identifiers —
    quoted ones included — at NAMEDATALEN-1 bytes, so without this two
    long CTE names differing only after byte 63 collide. SQL keywords
    are never this long and string literals are skipped, so the rewrite
    is name-renaming only."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("'"):
            return token  # string literal — data, never rewritten
        if token.startswith('"'):
            inner = token[1:-1].replace('""', '"')
            if len(inner.encode("utf-8")) <= _MAX_IDENTIFIER_BYTES:
                return token
            return '"' + _shortened_identifier(inner).replace('"', '""') + '"'
        if len(token.encode("utf-8")) <= _MAX_IDENTIFIER_BYTES:
            return token
        return _shortened_identifier(token)

    return _SQL_TOKEN_RE.sub(_replace, sql)


def _postgres_compat_sql(sql: str) -> str:
    # PostgreSQL integer division truncates (1 / 4 = 0) while DuckDB's
    # `/` is always float division; the shared pass casts the compiler's
    # NULLIF guard to PG's float type, DOUBLE PRECISION.
    return float_nullif_divisions(_shorten_long_identifiers(sql), cast_type="DOUBLE PRECISION")


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

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return super().query(_postgres_compat_sql(sql), limits=limits)

    def _apply_statement_timeout(self, cursor: Any, timeout_seconds: int) -> None:
        cursor.execute(f"SET statement_timeout = {int(timeout_seconds) * 1000}")

    def _reset_statement_timeout(self, cursor: Any) -> None:
        cursor.execute("RESET statement_timeout")


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
