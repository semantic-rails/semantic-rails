"""Arrow execution under the warehouse adapter contract.

Postgres is qualified; Snowflake (``snowflake_adbc``) is an opt-in experiment.
Credentials stay in memory and are never interpolated into query SQL.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..dialects import (
    POSTGRES_CONNECTION_OPTIONS,
    SNOWFLAKE_ADBC_CONNECTION_OPTIONS,
    backslash_escaped_string_literal,
)
from ..errors import SemanticLayerError, query_execution_error
from ..sql_preparation import (
    PreparedQuery,
    check_postgres_parameters,
    check_snowflake_parameters,
    checked_parameter_values,
    prepare_query,
)
from .base import (
    QueryRows,
    WarehouseAdapter,
    _limit_max_rows,
    _limit_timeout_milliseconds,
    restore_column_names,
)
from .common import (
    import_driver,
    int_option,
    normalize_connection_options,
    option_or_env,
    redacted_error_details,
    require_missing_env,
    secret_value,
    session_time_zone,
)


@dataclass(frozen=True)
class AdbcProfile:
    engine: str
    connection_kind: str
    driver: str
    connection_options: tuple[str, ...]


POSTGRES_PROFILE = AdbcProfile(
    "postgres", "postgres_native", "adbc_driver_postgresql.dbapi", POSTGRES_CONNECTION_OPTIONS
)
SNOWFLAKE_PROFILE = AdbcProfile(
    "snowflake", "snowflake_adbc", "adbc_driver_manager.dbapi", SNOWFLAKE_ADBC_CONNECTION_OPTIONS
)


class AdbcAdapter(WarehouseAdapter):
    engine = POSTGRES_PROFILE.engine
    connection_kind = POSTGRES_PROFILE.connection_kind
    supports_parameters = True
    supports_statement_timeout = True

    def __init__(
        self, options: dict[str, Any] | None = None, *, profile: AdbcProfile = POSTGRES_PROFILE
    ) -> None:
        if profile not in (POSTGRES_PROFILE, SNOWFLAKE_PROFILE):
            raise SemanticLayerError("INVALID_CONFIG", "Unsupported ADBC warehouse profile")
        self.profile = profile
        self.engine = profile.engine
        self.connection_kind = profile.connection_kind
        self.options = normalize_connection_options(
            self.engine, self.connection_kind, options or {}, profile.connection_options
        )
        self._conn: Any = None
        self._zone = "UTC"
        self._lock = threading.RLock()

    def _int_option(self, name: str, default: int) -> int:
        return int_option(
            self.options, name, default, engine=self.engine, connection_kind=self.connection_kind
        )

    def _connect_uri(self) -> str:
        missing: list[str] = []
        values = {name: option_or_env(self.options, name, missing) for name in ("host", "user")}
        values["password"] = secret_value(
            "password",
            self.options.get("password_env", ""),
            self.options.get("password_file", ""),
            missing,
            engine=self.engine,
            connection_kind=self.connection_kind,
        )
        require_missing_env(missing, engine=self.engine, connection_kind=self.connection_kind)
        values.update(port=str(self._int_option("port", 5432)), connect_timeout="10")
        for option, key in (("database", "dbname"), ("sslmode", "sslmode")):
            if self.options.get(option):
                values[key] = self.options[option]
        # libpq keyword values: quotes and backslashes are escaped, not SQL.
        return " ".join(
            key + "='" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
            for key, value in values.items()
            if value
        )

    def _snowflake_connect_options(self) -> dict[str, str]:
        missing: list[str] = []
        options = {
            "adbc.snowflake.sql.account": option_or_env(self.options, "account", missing),
            "username": option_or_env(self.options, "user", missing),
        }
        for name in ("password", "private_key", "private_key_passphrase"):
            value = secret_value(
                name,
                self.options.get(f"{name}_env", ""),
                self.options.get(f"{name}_file", ""),
                missing,
                engine=self.engine,
                connection_kind=self.connection_kind,
            )
            if value:
                key = {
                    "password": "password",
                    "private_key": "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_value",
                    "private_key_passphrase": "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_password",
                }[name]
                options[key] = value
        require_missing_env(missing, engine=self.engine, connection_kind=self.connection_kind)
        has_key = "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_value" in options
        if (
            not options["adbc.snowflake.sql.account"]
            or not options["username"]
            or (has_key == ("password" in options))
            or (self.options.get("private_key_passphrase_env") and not has_key)
        ):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Snowflake ADBC requires account, user and exactly one password or PKCS #8 key",
            )
        options["adbc.snowflake.sql.auth_type"] = "auth_jwt" if has_key else "auth_snowflake"
        for name, key in (
            ("database", "db"),
            ("schema", "schema"),
            ("warehouse", "warehouse"),
            ("role", "role"),
        ):
            if self.options.get(name):
                options[f"adbc.snowflake.sql.{key}"] = self.options[name]
        precision = self.options.get("use_high_precision", "true").lower()
        if precision not in ("true", "false"):
            raise SemanticLayerError("INVALID_CONFIG", "use_high_precision must be true or false")
        options["adbc.snowflake.sql.client_option.use_high_precision"] = precision
        return options

    def _connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        snowflake = self.profile == SNOWFLAKE_PROFILE
        credentials = self._snowflake_connect_options() if snowflake else None
        driver = import_driver(
            self.profile.driver,
            extra="snowflake-adbc" if snowflake else "postgres",
            engine=self.engine,
            connection_kind=self.connection_kind,
        )
        conn = (
            driver.connect(
                driver=self.options.get("driver_path", "snowflake"),
                db_kwargs=credentials,
                autocommit=True,
            )
            if snowflake
            else driver.connect(self._connect_uri(), autocommit=True)
        )
        try:
            with conn.cursor() as cursor:
                if snowflake:
                    if self.options.get("query_tag"):
                        cursor.execute(
                            "ALTER SESSION SET QUERY_TAG = "
                            + backslash_escaped_string_literal(self.options["query_tag"])
                        )
                    cursor.execute("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION")
                    self._zone = cursor.fetchone()[1]
                else:
                    if self.options.get("schema"):
                        schema = '"' + self.options["schema"].replace('"', '""') + '"'
                        cursor.execute("SELECT set_config('search_path', $1, false)", (schema,))
                    cursor.execute("SELECT current_setting('TimeZone')")
                    self._zone = cursor.fetchone()[0]
            self._conn = conn
        except Exception:
            conn.close()
            raise
        return conn

    def _setup_session(
        self, cursor: Any, timeout: int, limits: dict[str, Any] | None
    ) -> tuple[str, str, int]:
        if self.profile == SNOWFLAKE_PROFILE:
            cursor.execute("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION")
            original_zone = cursor.fetchone()[1]
            cursor.execute("SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION")
            original_timeout = int(cursor.fetchone()[1])
            zone = session_time_zone(limits) or original_zone
            cursor.execute(
                f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {(timeout + 999) // 1000}"
            )
            cursor.execute("ALTER SESSION SET TIMEZONE = " + backslash_escaped_string_literal(zone))
            return zone, original_zone, original_timeout
        cursor.execute("SELECT current_setting('TimeZone')")
        original_zone = cursor.fetchone()[0]
        zone = session_time_zone(limits) or original_zone
        cursor.execute("SELECT set_config('statement_timeout', $1, false)", (str(timeout),))
        cursor.execute("SELECT set_config('TimeZone', $1, false)", (zone,))
        return zone, original_zone, 0

    def _reset_session(self, cursor: Any, zone: str, timeout: int) -> None:
        if self.profile == SNOWFLAKE_PROFILE:
            cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout}")
            cursor.execute("ALTER SESSION SET TIMEZONE = " + backslash_escaped_string_literal(zone))
        else:
            cursor.execute("RESET statement_timeout")
            cursor.execute("SELECT set_config('TimeZone', $1, false)", (zone,))

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.query_prepared(prepare_query(sql, self.engine), limits=limits)

    def query_prepared(
        self,
        prepared: PreparedQuery,
        *,
        limits: dict[str, Any] | None = None,
        parameters: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        values = checked_parameter_values(prepared, parameters)
        if self.profile == SNOWFLAKE_PROFILE:
            check_snowflake_parameters(prepared)
        else:
            check_postgres_parameters(prepared)
        with self._lock:
            try:
                conn = self._connection()
                timeout = _limit_timeout_milliseconds(limits) or max(
                    0, self._int_option("statement_timeout_seconds", 0) * 1000
                )
                with conn.cursor() as cursor:
                    zone, original_zone, original_timeout = self._setup_session(
                        cursor, timeout, limits
                    )
                    finished = threading.Event()
                    cancel_errors: list[Exception] = []

                    def cancel() -> None:
                        if not finished.is_set():
                            try:
                                cursor.adbc_cancel()
                            except Exception as exc:
                                cancel_errors.append(exc)

                    watchdog = threading.Timer(timeout / 1000, cancel) if timeout else None
                    if watchdog:
                        watchdog.daemon = True
                        watchdog.start()
                    try:
                        if self.profile == POSTGRES_PROFILE:
                            cursor.adbc_statement.set_options(
                                **{"adbc.postgresql.batch_size_hint_bytes": "65536"}
                            )
                        # Prepared SQL is immutable here. Values go only to Arrow binding.
                        cursor.execute(prepared.sql, values or None)
                        rows = self._rows(cursor, limits, zone)
                    finally:
                        finished.set()
                        if watchdog:
                            watchdog.cancel()
                            watchdog.join()
                    if cancel_errors:
                        raise cancel_errors[0]
                    self._reset_session(cursor, original_zone, original_timeout)
                return restore_column_names(rows, prepared)
            except Exception as exc:
                # A cancelled COPY or failed reset must never leave a session reusable.
                with suppress(Exception):
                    self.close()
                if isinstance(exc, SemanticLayerError):
                    raise
                raise query_execution_error(
                    redacted_error_details(self.engine, self.connection_kind, self.options)
                ) from exc

    @staticmethod
    def _rows(cursor: Any, limits: dict[str, Any] | None, zone: str) -> QueryRows:
        cap = _limit_max_rows(limits)
        if limits and limits.get("max_rows") == 0:
            cap = 0
        rows: list[dict[str, Any]] = []
        with cursor.fetch_record_batch() as reader:
            numeric = {
                field.name
                for field in reader.schema
                if getattr(field.type, "type_name", "") == "numeric"
                and getattr(field.type, "vendor_name", "") == "PostgreSQL"
            }
            for batch in reader:
                if cap is not None:
                    batch = batch.slice(0, max(0, cap + 1 - len(rows)))
                for row in batch.to_pylist():
                    for key, value in row.items():
                        if value is not None and key in numeric:
                            row[key] = Decimal(value)
                        elif isinstance(value, datetime) and value.tzinfo is not None:
                            row[key] = value.astimezone(ZoneInfo(zone))
                    rows.append(row)
                if cap is not None and len(rows) > cap:
                    break
        truncated = cap is not None and len(rows) > cap
        return QueryRows(rows[:cap] if truncated else rows, truncated=truncated)

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()
