"""Databricks SQL warehouse adapter (``databricks_native``).

Backed by the DB-API ``databricks-sql-connector`` driver (module
``databricks.sql``; pip extra ``semantic-rails[databricks]``). The
adapter subclasses :class:`~semantic_rails.db_parts.common.DbApiAdapter`
so query execution, row mapping, limits, redacted errors, and close are
shared — only connection creation and the statement-timeout hooks live
here.

Connection contract (see ``DATABRICKS_CONNECTION_OPTIONS`` in
:mod:`semantic_rails.dialects`):

- ``host`` / ``host_env`` — workspace hostname; any ``https://`` scheme
  prefix is stripped before it is passed as ``server_hostname``.
- ``http_path`` / ``http_path_env`` — the SQL Warehouse HTTP path.
- ``token_env`` / ``token_file`` — personal access token (secrets are
  never literals in package YAML; normalization enforces this).
- ``catalog`` / ``schema`` — passed to ``databricks.sql.connect`` so
  UNQUALIFIED table names (e.g. ``jaffle_order``) resolve in the
  configured namespace.
"""

from __future__ import annotations

import re
from typing import Any

from ..dialects import DATABRICKS_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter
from .common import (
    DbApiAdapter,
    import_driver,
    normalize_connection_options,
    option_or_env,
    require_missing_env,
    secret_value,
)

_SCHEME_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _server_hostname(host: str) -> str:
    """Normalize a host option to the bare hostname the driver expects."""
    return _SCHEME_PREFIX_RE.sub("", str(host or "").strip()).strip("/")


class DatabricksNativeAdapter(DbApiAdapter):
    engine = "databricks"
    connection_kind = "databricks_native"
    # Honors limits.statement_timeout_ms via Databricks SQL's
    # session-level STATEMENT_TIMEOUT configuration parameter (seconds).
    supports_statement_timeout = True

    def __init__(self, options: dict[str, Any] | None = None):
        super().__init__()
        self.options = normalize_connection_options(
            "databricks",
            self.connection_kind,
            options or {},
            DATABRICKS_CONNECTION_OPTIONS,
            label="Databricks",
        )

    def _connect_kwargs(self) -> dict[str, Any]:
        missing_env: list[str] = []
        host = option_or_env(self.options, "host", missing_env)
        http_path = option_or_env(self.options, "http_path", missing_env)
        token_file = self.options.get("token_file", "")
        token = secret_value(
            "token",
            self.options.get("token_env", ""),
            token_file,
            missing_env if not token_file else None,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label="Databricks",
        )
        require_missing_env(
            missing_env,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label="Databricks",
        )
        # Option KEYS only in errors — never resolved values.
        missing_options = [
            name
            for name, value in (
                ("host (or host_env)", host),
                ("http_path (or http_path_env)", http_path),
                ("token_env (or token_file)", token),
            )
            if not value
        ]
        if missing_options:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Databricks connection is missing required options: " + ", ".join(missing_options),
                details={
                    "engine": self.engine,
                    "connection_kind": self.connection_kind,
                    "option_keys": sorted(self.options),
                },
            )
        kwargs: dict[str, Any] = {
            "server_hostname": _server_hostname(host),
            "http_path": http_path,
            "access_token": token,
        }
        # Default the namespace from connection options so unqualified
        # table names resolve without per-query qualification.
        for key in ("catalog", "schema"):
            if self.options.get(key):
                kwargs[key] = self.options[key]
        return kwargs

    def _create_connection(self) -> Any:
        driver = import_driver(
            "databricks.sql",
            extra="databricks",
            engine=self.engine,
            connection_kind=self.connection_kind,
        )
        return driver.connect(**self._connect_kwargs(), use_cloud_fetch=False)

    def _apply_statement_timeout(self, cursor: Any, timeout_seconds: int) -> None:
        cursor.execute(f"SET STATEMENT_TIMEOUT = {int(timeout_seconds)}")

    def _reset_statement_timeout(self, cursor: Any) -> None:
        cursor.execute("RESET STATEMENT_TIMEOUT")


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    """Registry entry point for the databricks warehouse (see dialects.py)."""
    kind = package.connection.kind
    if kind != "databricks_native":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported Databricks connection kind '{kind}'",
            details={"engine": "databricks", "connection_kind": str(kind)},
        )
    return DatabricksNativeAdapter(package.connection.options)
