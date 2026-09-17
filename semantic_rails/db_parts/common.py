"""Shared warehouse-adapter helpers.

Every concrete adapter (Snowflake, Postgres, BigQuery, …) resolves
secrets, normalizes connection options, maps driver rows to dicts, and
redacts errors the same way. This module is the single home for that
logic so a new adapter never copy-pastes it; see
``docs/ADDING_A_DIALECT.md``.

Conventions enforced here (mirroring the original Snowflake adapter):

- Package YAML never carries literal credential text. Secret-bearing
  options use env-var indirection (``*_env``) or file paths
  (``*_file``); literal ``password`` / ``token`` / ``private_key`` /
  ``secret`` / ``api_key`` keys are rejected at normalization time.
- Adapter-level errors never leak raw SQL, option values, or result
  rows. Error details carry the engine, connection kind, option KEYS,
  and ``sql_redacted: True`` only; raw driver text is never public.
"""

from __future__ import annotations

import importlib
import os
from abc import abstractmethod
from contextlib import suppress
from typing import Any

from ..dialects import (
    connection_option_errors,
    normalize_connection_option_name,
)
from ..errors import SemanticLayerError, query_execution_error
from ..sql_preparation import PreparedQuery, prepare_query
from .base import (
    QueryRows,
    WarehouseAdapter,
    _clip_rows,
    _limit_max_rows,
    _limit_timeout_seconds,
    restore_column_names,
)

# Hard contract: package YAML must never carry literal credential text.
# See docs/PACKAGE_AUTHORING.md "Secrets". Anything resembling a literal
# secret key is rejected regardless of connection kind.
FORBIDDEN_LITERAL_SECRET_KEYS = frozenset(
    {"password", "token", "private_key", "secret", "api_key", "credentials"}
)


def env_value(name: str, missing_env: list[str] | None = None) -> str:
    """Resolve an env-var-indirected option (``*_env``) to its value.

    Empty/unset variables are recorded in ``missing_env`` (when given)
    so the caller can raise ONE error naming every missing variable
    instead of failing piecemeal.
    """
    env_name = str(name or "").strip()
    if not env_name:
        return ""
    if env_name not in os.environ or os.environ.get(env_name, "") == "":
        if missing_env is not None:
            missing_env.append(env_name)
        return ""
    return os.environ.get(env_name, "")


def option_or_env(options: dict[str, str], name: str, missing_env: list[str] | None = None) -> str:
    """Resolve the ``<name>`` / ``<name>_env`` connection-option pair.

    A literal option wins; otherwise the companion ``<name>_env``
    variable is resolved through :func:`env_value` (recording unset
    variables in ``missing_env`` when given). This is the canonical
    non-secret locator convention (host, user, region, project, …) —
    see ``docs/ADDING_A_DIALECT.md``.
    """
    value = options.get(name, "")
    if value:
        return value
    return env_value(options.get(f"{name}_env", ""), missing_env)


def secret_value(
    option_name: str,
    env_name: str,
    file_name: str,
    missing_env: list[str] | None = None,
    *,
    engine: str = "",
    connection_kind: str = "",
    label: str = "",
) -> str:
    """Resolve a secret from env-var indirection, falling back to a file.

    The error path never includes the secret itself — only the option
    name and engine metadata.
    """
    value = env_value(env_name, missing_env)
    if value:
        return value
    if file_name:
        try:
            with open(file_name, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"Could not read {label or engine or 'warehouse'} secret file for option '{option_name}'",
                details={
                    "engine": engine,
                    "connection_kind": connection_kind,
                    "option": option_name,
                },
            ) from exc
    return ""


def normalize_connection_options(
    warehouse: str,
    kind: str,
    options: dict[str, Any],
    allowed: tuple[str, ...],
    *,
    label: str = "",
) -> dict[str, str]:
    """Validate and normalize a ``package.connection.options`` mapping.

    Rejects unknown options, non-string values, and literal secret keys,
    then returns the allowed subset with normalized key names and
    stripped string values. ``label`` is the human-facing connection
    name used in error messages (e.g. ``"Snowflake CLI"``).
    """
    display = label or f"{warehouse} {kind}".strip()
    errors = connection_option_errors(warehouse, kind, options)
    if errors:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{display} connection has invalid options: {'; '.join(errors)}",
        )
    for raw_key in options or {}:
        normalized_key = normalize_connection_option_name(str(raw_key))
        if normalized_key in FORBIDDEN_LITERAL_SECRET_KEYS:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{display} package.connection.options['{normalized_key}'] is not allowed; "
                f"use env-var indirection (e.g. '{normalized_key}_env: MY_ENV_VAR') or a file path. "
                "See docs/PACKAGE_AUTHORING.md#secrets.",
            )
    normalized: dict[str, str] = {}
    for raw_key, value in (options or {}).items():
        key = normalize_connection_option_name(str(raw_key))
        if key in allowed:
            normalized[key] = str(value).strip()
    return normalized


def require_missing_env(
    missing_env: list[str], *, engine: str, connection_kind: str, label: str = ""
) -> None:
    """Raise the canonical "missing env vars" error when any were recorded."""
    if not missing_env:
        return
    raise SemanticLayerError(
        "INVALID_CONFIG",
        f"{label or engine} connection is missing required environment values",
        details={
            "engine": engine,
            "connection_kind": connection_kind,
            "missing_env": sorted(set(missing_env)),
        },
    )


def int_option(
    options: dict[str, str],
    name: str,
    default: int,
    *,
    engine: str,
    connection_kind: str,
    label: str = "",
) -> int:
    """Parse an integer connection option, defaulting when unset.

    The error envelope carries the option KEY only — option VALUES
    never appear (they could be secrets routed to the wrong key).
    """
    raw = options.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{label or engine} package.connection option '{name}' must be an integer",
            details={
                "engine": engine,
                "connection_kind": connection_kind,
                "option": name,
            },
        ) from exc


def redacted_error_details(
    engine: str, connection_kind: str, options: dict[str, str]
) -> dict[str, Any]:
    """The canonical redacted QUERY_EXECUTION_ERROR details payload.

    Engine, connection kind, option KEYS, and the ``sql_redacted``
    marker only — never option values, raw SQL, or result rows.
    """
    return {
        "engine": engine,
        "connection_kind": connection_kind,
        "option_keys": sorted(options),
        "sql_redacted": True,
    }


def import_driver(module_name: str, *, extra: str, engine: str, connection_kind: str) -> Any:
    """Import an optional warehouse driver, mapping ImportError to the
    canonical MISSING_DEPENDENCY envelope naming the pip extra."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SemanticLayerError(
            "MISSING_DEPENDENCY",
            f"Install semantic-rails[{extra}] to use package.connection.kind {connection_kind}.",
            details={"engine": engine, "connection_kind": connection_kind},
        ) from exc


def rows_from_cursor(cursor: Any, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Map a DB-API cursor's fetched rows to a list of dicts."""
    columns = [str(col[0]) for col in list(cursor.description or [])]
    cap = _limit_max_rows(limits)
    if cap is not None and hasattr(cursor, "fetchmany"):
        fetched = cursor.fetchmany(cap + 1)
    else:
        fetched = cursor.fetchall()
    truncated = cap is not None and len(fetched) > cap
    if truncated:
        fetched = fetched[:cap]
    return QueryRows(
        [dict(zip(columns, row, strict=False)) for row in fetched],
        truncated=truncated,
    )


class DbApiAdapter(WarehouseAdapter):
    """Base class for adapters backed by a PEP 249 (DB-API 2.0) driver.

    Subclasses implement :meth:`_create_connection` (and usually set
    ``engine`` / ``connection_kind`` / ``supports_statement_timeout``).
    Everything else — lazy connect, cursor lifecycle, row mapping,
    ``limits`` enforcement, redacted error envelopes, close — is shared.

    Statement timeouts: subclasses that can enforce
    ``limits.statement_timeout_ms`` set ``supports_statement_timeout =
    True`` and override :meth:`_apply_statement_timeout` /
    :meth:`_reset_statement_timeout` with the warehouse-native mechanism
    (e.g. Postgres ``SET statement_timeout``).
    """

    engine = "dbapi"
    connection_kind = ""

    def __init__(self) -> None:
        self._conn: Any = None
        self.options: dict[str, str] = {}

    @abstractmethod
    def _create_connection(self) -> Any:
        """Return a new live driver connection (called lazily, once)."""
        raise NotImplementedError

    def _connection(self) -> Any:
        if self._conn is None:
            self._conn = self._create_connection()
        return self._conn

    def _apply_statement_timeout(self, cursor: Any, timeout_seconds: int) -> None:
        """Set a session/statement timeout before executing. No-op default."""

    def _reset_statement_timeout(self, cursor: Any) -> None:
        """Undo :meth:`_apply_statement_timeout`. No-op default."""

    def _error_details(self) -> dict[str, Any]:
        return redacted_error_details(self.engine, self.connection_kind, self.options)

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.query_prepared(prepare_query(sql, self.engine), limits=limits)

    def query_prepared(
        self, prepared: PreparedQuery, *, limits: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        timeout_s = _limit_timeout_seconds(limits)
        use_timeout = timeout_s > 0 and self.supports_statement_timeout
        try:
            cursor = self._connection().cursor()
            try:
                if use_timeout:
                    self._apply_statement_timeout(cursor, timeout_s)
                cursor.execute(prepared.sql)
                rows = rows_from_cursor(cursor, limits=limits)
                return restore_column_names(_clip_rows(rows, limits), prepared)
            finally:
                if use_timeout:
                    with suppress(Exception):
                        self._reset_statement_timeout(cursor)
                cursor.close()
        except SemanticLayerError:
            raise
        except Exception as exc:
            raise query_execution_error(self._error_details()) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
