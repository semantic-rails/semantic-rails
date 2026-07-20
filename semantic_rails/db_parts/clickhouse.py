"""ClickHouse warehouse adapter.

Uses ``clickhouse-connect`` (HTTP protocol). The driver is NOT DB-API
(PEP 249) — it exposes a client object with ``query``/``insert`` — so
this adapter subclasses :class:`WarehouseAdapter` directly while
reusing the shared :mod:`semantic_rails.db_parts.common` helpers for
option normalization, secret resolution, lazy driver import, and
redacted errors.

Statement timeouts are enforced natively via the per-query
``max_execution_time`` setting, so ``supports_statement_timeout`` is
truthfully ``True``.

The client session sets ``allow_experimental_join_condition=1``:
conversion-window queries join on ``key <=> key AND time > base_time``
and ClickHouse 24.x rejects the non-equi part of a JOIN ON without it
(verified live on 24.8).
"""

from __future__ import annotations

from typing import Any

from ..dialects import CLICKHOUSE_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter, _clip_rows, _limit_timeout_seconds
from .common import (
    bounded_error_text,
    import_driver,
    int_option,
    normalize_connection_options,
    option_or_env,
    redacted_error_details,
    require_missing_env,
    secret_value,
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ClickHouseAdapter(WarehouseAdapter):
    engine = "clickhouse"
    connection_kind = "clickhouse_native"
    # Honors limits.statement_timeout_ms via the per-query
    # max_execution_time setting (ClickHouse aborts the statement
    # server-side when it is exceeded).
    supports_statement_timeout = True

    def __init__(self, options: dict[str, Any] | None = None):
        self.options = normalize_connection_options(
            "clickhouse",
            self.connection_kind,
            options or {},
            CLICKHOUSE_CONNECTION_OPTIONS,
            label="ClickHouse",
        )
        self._client: Any = None

    def _connect_kwargs(self) -> dict[str, Any]:
        missing: list[str] = []
        host = option_or_env(self.options, "host", missing)
        user = option_or_env(self.options, "user", missing)
        password = secret_value(
            "password",
            self.options.get("password_env", ""),
            self.options.get("password_file", ""),
            missing,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label="ClickHouse",
        )
        require_missing_env(
            missing, engine=self.engine, connection_kind=self.connection_kind, label="ClickHouse"
        )
        port = int_option(
            self.options,
            "port",
            8123,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label="ClickHouse",
        )
        kwargs: dict[str, Any] = {
            "host": host or "localhost",
            "port": port,
            "username": user or "default",
            "password": password,
            "secure": str(self.options.get("secure", "")).strip().lower() in _TRUTHY,
            # Non-equi JOIN ON conditions (conversion-window joins) need
            # this on ClickHouse 24.x; see module docstring.
            "settings": {"allow_experimental_join_condition": 1},
        }
        if self.options.get("database"):
            # Default namespace so unqualified table names (jaffle_order,
            # …) resolve without package-side qualification.
            kwargs["database"] = self.options["database"]
        return kwargs

    def _client_handle(self) -> Any:
        if self._client is None:
            driver = import_driver(
                "clickhouse_connect",
                extra="clickhouse",
                engine=self.engine,
                connection_kind=self.connection_kind,
            )
            self._client = driver.get_client(**self._connect_kwargs())
        return self._client

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        timeout_s = _limit_timeout_seconds(limits)
        settings = {"max_execution_time": timeout_s} if timeout_s > 0 else None
        try:
            result = self._client_handle().query(sql, settings=settings)
            rows = [dict(zip(result.column_names, row, strict=False)) for row in result.result_rows]
            return _clip_rows(rows, limits)
        except SemanticLayerError:
            raise
        except Exception as exc:
            # Redacted error envelope: engine, connection kind, option
            # KEYS, bounded driver text — never option values, raw SQL,
            # or result rows.
            raise SemanticLayerError(
                "QUERY_EXECUTION_ERROR",
                f"ClickHouse query execution failed: {bounded_error_text(str(exc))}",
                details=redacted_error_details(self.engine, self.connection_kind, self.options),
            ) from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    return ClickHouseAdapter(dict(getattr(package.connection, "options", {}) or {}))
