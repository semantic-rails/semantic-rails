"""MotherDuck warehouse adapter.

MotherDuck speaks DuckDB SQL over the ``md:`` cloud transport, so the
dialect (:class:`semantic_rails.dialects.MotherDuckDialect`) inherits
everything from :class:`DuckDbDialect` and this module only owns the
connection: token resolution via env/file indirection, lazy database
creation, and namespace defaulting so unqualified table names (e.g.
``jaffle_order``) resolve against the configured database/schema.

The driver is the core ``duckdb`` dependency (no pip extra) — imported
with the same guarded pattern as :mod:`semantic_rails.db`.
"""

from __future__ import annotations

import re
from contextlib import suppress
from typing import Any

try:
    import duckdb
except ImportError:  # pragma: no cover - core dependency, mirrors semantic_rails.db
    duckdb = None  # type: ignore[assignment]  # optional dependency fallback

from ..dialects import MOTHERDUCK_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter
from .common import (
    DbApiAdapter,
    normalize_connection_options,
    require_missing_env,
    secret_value,
)

# Database/schema names are interpolated into CREATE DATABASE / USE
# statements, so they must be plain identifiers. The check never echoes
# the value back — error details carry the option KEY only.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _require_identifier(option_name: str, value: str) -> str:
    if not _IDENTIFIER_RE.match(value or ""):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"MotherDuck connection option '{option_name}' must be a simple identifier "
            "(letters, digits, underscores; starting with a letter or underscore)",
            details={
                "engine": "motherduck",
                "connection_kind": "motherduck_native",
                "option": option_name,
            },
        )
    return value


class MotherDuckAdapter(DbApiAdapter):
    """DB-API-ish adapter over ``duckdb.connect("md:...")``.

    DuckDB connections satisfy enough of PEP 249 for the shared
    :class:`DbApiAdapter` machinery (``cursor()``, ``execute``,
    ``description``, ``fetchall``, ``close``).
    """

    engine = "motherduck"
    connection_kind = "motherduck_native"
    # TRUTHFUL: MotherDuck has no server-side statement timeout, so
    # limits.statement_timeout_ms stays best-effort (the runtime warns).
    supports_statement_timeout = False

    def __init__(self, options: dict[str, Any] | None = None):
        super().__init__()
        self.options = normalize_connection_options(
            "motherduck",
            self.connection_kind,
            options or {},
            MOTHERDUCK_CONNECTION_OPTIONS,
            label="MotherDuck",
        )
        missing_options = [
            key
            for key, present in (
                ("database", bool(self.options.get("database"))),
                (
                    "token_env or token_file",
                    bool(self.options.get("token_env") or self.options.get("token_file")),
                ),
            )
            if not present
        ]
        if missing_options:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "MotherDuck connection is incomplete: requires " + ", ".join(missing_options),
                details={
                    "engine": self.engine,
                    "connection_kind": self.connection_kind,
                    "option_keys": sorted(self.options),
                },
            )

    def _create_connection(self) -> Any:
        if duckdb is None:  # pragma: no cover - core dependency
            raise SemanticLayerError(
                "MISSING_DEPENDENCY",
                "duckdb is not installed. Add it to your environment dependencies "
                f"to use package.connection.kind {self.connection_kind}.",
                details={"engine": self.engine, "connection_kind": self.connection_kind},
            )
        missing_env: list[str] = []
        token_file = self.options.get("token_file", "")
        token = secret_value(
            "token",
            self.options.get("token_env", ""),
            token_file,
            missing_env if not token_file else None,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label="MotherDuck",
        )
        require_missing_env(
            missing_env,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label="MotherDuck",
        )
        if not token:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "MotherDuck connection token resolved empty; "
                "set the token_env variable or provide a non-empty token_file",
                details={
                    "engine": self.engine,
                    "connection_kind": self.connection_kind,
                    "option_keys": sorted(self.options),
                },
            )
        database = _require_identifier("database", self.options.get("database", ""))
        schema = self.options.get("schema", "")
        if schema:
            _require_identifier("schema", schema)
        # Connect to the bare `md:` workspace first so the configured
        # database can be created lazily, then make it (and the optional
        # schema) the session default — unqualified table names resolve.
        conn = duckdb.connect("md:", config={"motherduck_token": token})
        try:
            conn.execute(f'CREATE DATABASE IF NOT EXISTS "{database}"')
            conn.execute(f'USE "{database}"')
            if schema:
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                conn.execute(f'USE "{database}"."{schema}"')
        except Exception:
            with suppress(Exception):
                conn.close()
            raise
        return conn


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    """Registry entry point for the motherduck warehouse (see dialects.py)."""
    kind = str(getattr(package.connection, "kind", "") or "").strip()
    if kind != "motherduck_native":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported MotherDuck connection kind '{kind}'",
            details={"engine": "motherduck"},
        )
    return MotherDuckAdapter(package.connection.options)
