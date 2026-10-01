"""Arrow execution under the warehouse adapter contract.

Postgres is qualified; Snowflake (``snowflake_adbc``) is opt-in.
Credentials stay in memory and are never interpolated into query SQL.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _check_postgres_result_types(schema: Any) -> None:
    """Allow only exact scalar mappings, before reading even null or empty results."""
    if not len(schema):
        return
    pa = import_driver(
        "pyarrow", extra="postgres", engine="postgres", connection_kind="postgres_native"
    )
    for field in schema:
        data_type = field.type
        numeric = (
            isinstance(data_type, pa.OpaqueType)
            and data_type.type_name == "numeric"
            and data_type.vendor_name == "PostgreSQL"
            and data_type.storage_type == pa.string()
        )
        if (
            numeric
            or pa.types.is_integer(data_type)
            or pa.types.is_decimal(data_type)
            or pa.types.is_float32(data_type)
            or pa.types.is_float64(data_type)
            or pa.types.is_string(data_type)
            or pa.types.is_boolean(data_type)
            or pa.types.is_date32(data_type)
            or (pa.types.is_timestamp(data_type) and data_type.unit == "us")
            or data_type == pa.month_day_nano_interval()
            or pa.types.is_null(data_type)
        ):
            continue
        raise SemanticLayerError(
            "RESULT_TYPE_UNSUPPORTED",
            f"Postgres result column {field.name!r} has unsupported Arrow type {data_type}.",
            details={"column": field.name, "type": str(data_type)},
        )


def _postgres_value(value: Any, data_type: Any, result_zone: tzinfo) -> Any:
    """Convert a driver value by its Arrow type, never by the text's appearance."""
    if (
        value is not None
        and getattr(data_type, "type_name", "") == "numeric"
        and getattr(data_type, "vendor_name", "") == "PostgreSQL"
    ):
        return Decimal(value)
    if value is not None and str(data_type) == "month_day_nano_interval":
        if value.nanoseconds % 1000 == 0:
            try:
                # Match DuckDB's driver duration: a month becomes 30 days.
                return timedelta(
                    days=value.months * 30 + value.days, microseconds=value.nanoseconds // 1000
                )
            except OverflowError:
                pass
        raise SemanticLayerError(
            "RESULT_VALUE_UNSUPPORTED",
            "A result interval cannot be represented as an exact Python timedelta.",
        )
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(result_zone)
    return value


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
        for name, key in (
            ("password", "password"),
            ("private_key", "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_value"),
            (
                "private_key_passphrase",
                "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_password",
            ),
        ):
            value = secret_value(
                name,
                self.options.get(f"{name}_env", ""),
                self.options.get(f"{name}_file", ""),
                missing,
                engine=self.engine,
                connection_kind=self.connection_kind,
            )
            if value:
                options[key] = value
        require_missing_env(missing, engine=self.engine, connection_kind=self.connection_kind)
        has_key = "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_value" in options
        if (
            not options["adbc.snowflake.sql.account"]
            or not options["username"]
            or has_key == ("password" in options)
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
                elif self.options.get("schema"):
                    schema = '"' + self.options["schema"].replace('"', '""') + '"'
                    cursor.execute("SELECT set_config('search_path', $1, false)", (schema,))
            self._conn = conn
        except Exception:
            conn.close()
            raise
        return conn

    def _session_settings(self, cursor: Any) -> tuple[str, str]:
        if self.profile == SNOWFLAKE_PROFILE:
            cursor.execute("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION")
            zone = cursor.fetchone()[1]
            cursor.execute("SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION")
            return zone, cursor.fetchone()[1]
        cursor.execute("SELECT current_setting('TimeZone'), current_setting('statement_timeout')")
        zone, timeout = cursor.fetchone()
        return zone, timeout

    def _set_timeout(self, cursor: Any, timeout: str) -> None:
        if self.profile == SNOWFLAKE_PROFILE:
            cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(timeout)}")
        else:
            cursor.execute("SELECT set_config('statement_timeout', $1, false)", (timeout,))

    def _set_zone(self, cursor: Any, zone: str) -> None:
        if self.profile == SNOWFLAKE_PROFILE:
            cursor.execute("ALTER SESSION SET TIMEZONE = " + backslash_escaped_string_literal(zone))
        else:
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
        snowflake = self.profile == SNOWFLAKE_PROFILE
        if snowflake:
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
                    original_zone, original_timeout = self._session_settings(cursor)
                    zone = session_time_zone(limits) or original_zone
                    if timeout:
                        self._set_timeout(
                            cursor, str((timeout + 999) // 1000 if snowflake else timeout)
                        )
                    if zone != original_zone:
                        self._set_zone(cursor, zone)
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
                        if not snowflake:
                            cursor.adbc_statement.set_options(
                                **{"adbc.postgresql.batch_size_hint_bytes": "65536"}
                            )
                        # Prepared SQL is immutable here. Values go only to Arrow binding.
                        cursor.execute(prepared.sql, values or None)
                        rows = self._rows(cursor, limits, zone, profile=self.profile)
                    finally:
                        finished.set()
                        if watchdog:
                            watchdog.cancel()
                            watchdog.join()
                    if cancel_errors:
                        raise cancel_errors[0]
                    if timeout:
                        self._set_timeout(cursor, original_timeout)
                    if zone != original_zone:
                        self._set_zone(cursor, original_zone)
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
    def _rows(
        cursor: Any,
        limits: dict[str, Any] | None,
        zone: str,
        *,
        profile: AdbcProfile = POSTGRES_PROFILE,
    ) -> QueryRows:
        cap = _limit_max_rows(limits)
        result_zone: tzinfo
        try:
            result_zone = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            result_zone = UTC
        rows: list[dict[str, Any]] = []
        with cursor.fetch_record_batch() as reader:
            if profile == POSTGRES_PROFILE:
                _check_postgres_result_types(reader.schema)
            data_types = {field.name: field.type for field in reader.schema}
            for batch in reader:
                if cap is not None:
                    batch = batch.slice(0, max(0, cap + 1 - len(rows)))
                for row in batch.to_pylist():
                    for key, value in row.items():
                        if profile == POSTGRES_PROFILE:
                            row[key] = _postgres_value(value, data_types.get(key), result_zone)
                        elif isinstance(value, datetime) and value.tzinfo is not None:
                            row[key] = value.astimezone(result_zone)
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
