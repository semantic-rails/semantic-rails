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
  and ``sql_redacted: True`` only; stderr-ish driver text is bounded.
"""

from __future__ import annotations

import importlib
import os
import re
from abc import abstractmethod
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from ..dialects import (
    connection_option_errors,
    normalize_connection_option_name,
)
from ..errors import SemanticLayerError
from .base import QueryRows, WarehouseAdapter, _clip_rows, _limit_max_rows, _limit_timeout_seconds

_ERROR_TEXT_MAX_CHARS = 500

# Hard contract: package YAML must never carry literal credential text.
# See docs/PACKAGE_AUTHORING.md "Secrets". Anything resembling a literal
# secret key is rejected regardless of connection kind.
FORBIDDEN_LITERAL_SECRET_KEYS = frozenset(
    {"password", "token", "private_key", "secret", "api_key", "credentials"}
)


def bounded_error_text(text: str) -> str:
    cleaned = str(text or "").strip()
    if len(cleaned) <= _ERROR_TEXT_MAX_CHARS:
        return cleaned
    return cleaned[:_ERROR_TEXT_MAX_CHARS] + " … [truncated]"


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


def map_double_quoted_identifiers(sql: str, replace: Callable[[str], str]) -> str:
    """Quote-aware scanning core for identifier-re-quoting compat passes.

    The renderer emits ANSI ``"identifier"`` quoting; the compiler
    renders string LITERALS with single quotes only, so a quote-aware
    scan (single-quoted spans copied verbatim, including ``''``
    escapes) can safely rewrite every double-quoted span. Each
    identifier's inner text (with ``""`` escapes collapsed) is passed
    to ``replace``; its return value is emitted verbatim in place of
    the original quoted span. Callers own the target quoting/escaping
    (see :func:`rewrite_double_quoted_identifiers` and the BigQuery
    adapter's alias-legalizing variant).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            ident: list[str] = []
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        ident.append('"')
                        j += 2
                        continue
                    break
                ident.append(sql[j])
                j += 1
            out.append(replace("".join(ident)))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def rewrite_double_quoted_identifiers(sql: str, *, quote: str = "`") -> str:
    """Re-quote double-quoted identifiers for backtick dialects.

    Spark SQL and BigQuery treat double quotes as string literals and
    need backticks; see :func:`map_double_quoted_identifiers` for the
    scanning contract.
    """
    return map_double_quoted_identifiers(
        sql, lambda name: quote + name.replace(quote, quote + quote) + quote
    )


_DIVIDE_NULLIF_RE = re.compile(r"/\s*NULLIF\s*\(")
_SINGLE_QUOTED_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def literal_spans(sql: str) -> list[tuple[int, int]]:
    """Spans of single-quoted string literals (with ``''`` escapes)."""
    return [m.span() for m in _SINGLE_QUOTED_LITERAL_RE.finditer(sql)]


def float_nullif_divisions(sql: str, *, cast_type: str = "DOUBLE") -> str:
    """Make ``x / NULLIF(y, 0)`` divide as floats, matching DuckDB.

    The compiler guards every ratio it emits with exactly this idiom,
    and DuckDB's ``/`` is always float division. Several warehouses
    diverge — integer division truncates (Postgres, Trino: ``1 / 4 =
    0``) or DECIMAL division loses scale (Spark, Trino) — so
    count-over-count ratios (conversion rates, shares) silently
    collapse or lose precision. Rewriting the guard to
    ``CAST(NULLIF(y, 0) AS <cast_type>)`` promotes the whole division
    to floating point with no effect on already-float ratios.
    ``cast_type`` is the warehouse's float-ish type — ``DOUBLE``
    (Spark, Trino) or ``DOUBLE PRECISION`` (Postgres).

    Quote-aware paren matching; ``/ NULLIF(`` occurrences inside
    single-quoted literals are skipped and nested occurrences are
    rewritten recursively.
    """
    spans = literal_spans(sql)

    def _inside_literal(position: int) -> bool:
        return any(start <= position < end for start, end in spans)

    out: list[str] = []
    pos = 0
    while True:
        match = _DIVIDE_NULLIF_RE.search(sql, pos)
        if match is None:
            out.append(sql[pos:])
            return "".join(out)
        if _inside_literal(match.start()):
            out.append(sql[pos : match.end()])
            pos = match.end()
            continue
        depth = 1
        index = match.end()
        in_string = False
        while index < len(sql) and depth:
            char = sql[index]
            if in_string:
                if char == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 1  # escaped quote inside the literal
                    else:
                        in_string = False
            elif char == "'":
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth:  # unbalanced parens — leave the tail untouched
            out.append(sql[pos:])
            return "".join(out)
        out.append(sql[pos : match.start()])
        out.append("/ CAST(NULLIF(")
        out.append(float_nullif_divisions(sql[match.end() : index - 1], cast_type=cast_type))
        out.append(f") AS {cast_type})")
        pos = index


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
        timeout_s = _limit_timeout_seconds(limits)
        use_timeout = timeout_s > 0 and self.supports_statement_timeout
        try:
            cursor = self._connection().cursor()
            try:
                if use_timeout:
                    self._apply_statement_timeout(cursor, timeout_s)
                cursor.execute(sql)
                rows = rows_from_cursor(cursor, limits=limits)
                return _clip_rows(rows, limits)
            finally:
                if use_timeout:
                    with suppress(Exception):
                        self._reset_statement_timeout(cursor)
                cursor.close()
        except SemanticLayerError:
            raise
        except Exception as exc:
            raise SemanticLayerError(
                "QUERY_EXECUTION_ERROR",
                f"{self.engine} query execution failed: {bounded_error_text(str(exc))}",
                details=self._error_details(),
            ) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
