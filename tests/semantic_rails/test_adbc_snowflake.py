"""Driver-free Snowflake profile checks; no warehouse credentials required."""

import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import parse_config_report, resolve_package_reference
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.adbc import SNOWFLAKE_PROFILE, AdbcAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.result_values import result_rows
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_preparation import ParameterSlot, PreparedQuery
from tests.semantic_rails.test_adbc_adapter import Batch, Reader
from tests.semantic_rails.test_row_filters import OWN_ORDERS, _package

SLOT = ParameterSlot("tenant", "string")
OPTIONS = {"account_env": "TEST_ACCOUNT", "user_env": "TEST_USER", "password_env": "TEST_PASSWORD"}


class Cursor:
    def __init__(self, calls, failure=""):
        self.calls = calls
        self.failure = failure
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        self.sql = sql
        if sql == self.failure:
            raise RuntimeError("driver-secret-canary")

    def fetchone(self):
        if "STATEMENT_TIMEOUT" in self.sql:
            return ("STATEMENT_TIMEOUT_IN_SECONDS", "42")
        return ("TIMEZONE", "UTC")

    def fetch_record_batch(self):
        if self.failure == "fetch":
            raise RuntimeError("driver-secret-canary")
        return Reader(
            [Batch([{"physical": Decimal("123.4500"), "z": datetime(2026, 1, 1, tzinfo=UTC)}])]
        )

    def adbc_cancel(self):
        self.calls.append("cancel")
        if self.failure == "cancel":
            raise RuntimeError("driver-secret-canary")


@pytest.fixture
def driver(monkeypatch):
    calls = []
    for key, value in (
        ("TEST_ACCOUNT", "test-account"),
        ("TEST_USER", "test-user"),
        ("TEST_PASSWORD", "password-canary"),
    ):
        monkeypatch.setenv(key, value)

    def connect(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(cursor=lambda: Cursor(calls), close=lambda: calls.append("closed"))

    monkeypatch.setattr(
        "semantic_rails.db_parts.adbc.import_driver",
        lambda *args, **kwargs: SimpleNamespace(connect=connect),
    )
    return calls


def test_snowflake_dispatch_and_package_loading_without_named_profile(tmp_path):
    root = _package(tmp_path / "filtered", [OWN_ORDERS])
    path = root / "package.yml"
    payload = yaml.safe_load(path.read_text())
    payload["package"].update(
        warehouse="snowflake", connection={"kind": "snowflake_adbc", "options": OPTIONS}
    )
    payload["package"].pop("default_db", None)
    payload["package"].pop("seed", None)
    path.write_text(yaml.safe_dump(payload))
    config = load_package_config(str(root))
    adapter = create_warehouse_adapter(config.package)
    assert isinstance(adapter, AdbcAdapter)
    assert adapter.profile == SNOWFLAKE_PROFILE
    assert (adapter.engine, adapter.connection_kind) == ("snowflake", "snowflake_adbc")
    assert adapter.supports_parameters and adapter.supports_statement_timeout
    adapter.close()


def test_config_report_accepts_unnamed_snowflake_adbc(tmp_path):
    from tests.semantic_rails.test_config_validation import (
        _write_minimal_snowflake_package,
        _write_yaml,
    )

    root = tmp_path / "snowflake_adbc_demo"
    _write_minimal_snowflake_package(root)
    payload = yaml.safe_load((root / "package.yml").read_text())
    payload["package"]["connection"] = {"kind": "snowflake_adbc", "options": OPTIONS}
    _write_yaml(root / "package.yml", payload)
    report, _ = parse_config_report(resolve_package_reference(path=str(root)))
    assert report["ok"] is True, report["errors"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("driver_path", "../../package-driver.dylib"),
        ("driver-path", "/opt/package-driver.so"),
        ("driver_path", "package-driver.toml"),
        ("driver", "package-driver"),
        ("driver_name", "package-driver"),
        ("driver_manifest", "package-driver.toml"),
    ],
)
@pytest.mark.parametrize("inline", [False, True])
def test_package_driver_selection_refuses_at_load(tmp_path, key, value, inline):
    root = _package(tmp_path / "filtered", [OWN_ORDERS])
    path = root / "package.yml"
    payload = yaml.safe_load(path.read_text())
    connection = {"kind": "snowflake_adbc", "options": dict(OPTIONS)}
    (connection if inline else connection["options"])[key] = value
    payload["package"].update(warehouse="snowflake", connection=connection)
    path.write_text(yaml.safe_dump(payload))
    with pytest.raises(SemanticLayerError) as caught:
        load_package_config(str(root))
    assert caught.value.code == "INVALID_CONFIG"
    assert key in str(caught.value)
    report, _ = parse_config_report(resolve_package_reference(path=str(root)))
    assert report["ok"] is False


def test_password_file_mapping(driver, tmp_path):
    path = tmp_path / "password"
    path.write_text("file-password-canary\n")
    options = {key: value for key, value in OPTIONS.items() if key != "password_env"}
    adapter = AdbcAdapter({**options, "password_file": str(path)}, profile=SNOWFLAKE_PROFILE)
    adapter._connection()
    assert driver[0]["db_kwargs"]["password"] == "file-password-canary"
    adapter.close()


@pytest.mark.parametrize(
    "driver_path", [None, "/opt/drivers/libsnowflake.so", "/opt/snowflake.toml"]
)
@pytest.mark.parametrize("precision", [None, "true", "false"])
def test_password_options_search_path_precision_and_escaped_tag(
    driver, monkeypatch, driver_path, precision
):
    options = {
        **OPTIONS,
        "database": "DB",
        "schema": "PUBLIC",
        "warehouse": "WH",
        "role": "ROLE",
        "query_tag": "canary\\'; SELECT 1 --",
    }
    if driver_path:
        monkeypatch.setenv("SR_SNOWFLAKE_ADBC_DRIVER_PATH", driver_path)
    else:
        monkeypatch.delenv("SR_SNOWFLAKE_ADBC_DRIVER_PATH", raising=False)
    if precision:
        options["use_high_precision"] = precision
    adapter = AdbcAdapter(options, profile=SNOWFLAKE_PROFILE)
    adapter._connection()
    assert driver[0] == {
        "driver": driver_path or "snowflake",
        "autocommit": True,
        "db_kwargs": {
            "adbc.snowflake.sql.account": "test-account",
            "username": "test-user",
            "password": "password-canary",
            "adbc.snowflake.sql.auth_type": "auth_snowflake",
            "adbc.snowflake.sql.db": "DB",
            "adbc.snowflake.sql.schema": "PUBLIC",
            "adbc.snowflake.sql.warehouse": "WH",
            "adbc.snowflake.sql.role": "ROLE",
            "adbc.snowflake.sql.client_option.use_high_precision": precision or "true",
            "adbc.snowflake.sql.client_option.max_timestamp_precision": "nanoseconds_error_on_overflow",
        },
    }
    assert driver[1] == ("ALTER SESSION SET QUERY_TAG = 'canary\\\\''; SELECT 1 --'", None)
    assert all("password-canary" not in sql for sql, _ in driver[1:])
    adapter.close()


@pytest.mark.parametrize("source", ["env", "file"])
@pytest.mark.parametrize("encrypted", [False, True])
def test_pkcs8_key_pair_mapping_without_password(driver, monkeypatch, tmp_path, source, encrypted):
    options = {key: value for key, value in OPTIONS.items() if key != "password_env"}
    label = "ENCRYPTED PRIVATE KEY" if encrypted else "PRIVATE KEY"
    key = f"-----BEGIN {label}-----\nsynthetic-key-canary\n-----END {label}-----"
    if source == "env":
        monkeypatch.setenv("TEST_KEY", key)
        options["private_key_env"] = "TEST_KEY"
    else:
        path = tmp_path / "key.p8"
        path.write_text(key)
        options["private_key_file"] = str(path)
    if encrypted:
        monkeypatch.setenv("TEST_PASSPHRASE", "passphrase-canary")
        options["private_key_passphrase_env"] = "TEST_PASSPHRASE"
    adapter = AdbcAdapter(options, profile=SNOWFLAKE_PROFILE)
    adapter._connection()
    mapped = driver[0]["db_kwargs"]
    assert mapped["adbc.snowflake.sql.auth_type"] == "auth_jwt"
    assert mapped["adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_value"] == key
    assert "password" not in mapped
    password_key = "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_password"
    assert mapped.get(password_key) == ("passphrase-canary" if encrypted else None)
    assert all("synthetic-key-canary" not in sql for sql, _ in driver[1:])
    adapter.close()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"password": "literal-secret-canary"},
        {"token_env": "TOKEN"},
        {**OPTIONS, "use_high_precision": "not-a-boolean"},
        {**OPTIONS, "private_key_env": "TEST_KEY"},
        {"user_env": "TEST_USER", "password_env": "TEST_PASSWORD"},
        {**OPTIONS, "private_key_passphrase_env": "TEST_KEY"},
    ],
)
def test_invalid_auth_and_options_refused_before_driver_connect(options, driver, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "key-canary")
    with pytest.raises(SemanticLayerError) as caught:
        AdbcAdapter(options, profile=SNOWFLAKE_PROFILE)._connection()
    assert caught.value.code == "INVALID_CONFIG"
    assert "literal-secret-canary" not in str(caught.value)
    assert not driver


def test_missing_env_refused_before_connect(driver, monkeypatch):
    monkeypatch.delenv("TEST_PASSWORD")
    with pytest.raises(SemanticLayerError) as caught:
        AdbcAdapter(OPTIONS, profile=SNOWFLAKE_PROFILE)._connection()
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.details["missing_env"] == ["TEST_PASSWORD"]
    assert not driver


def test_missing_python_driver_names_opt_in_extra(monkeypatch):
    import sys

    for key, value in (
        ("TEST_ACCOUNT", "account"),
        ("TEST_USER", "user"),
        ("TEST_PASSWORD", "synthetic-password"),
    ):
        monkeypatch.setenv(key, value)
    monkeypatch.setitem(sys.modules, "adbc_driver_manager.dbapi", None)
    with pytest.raises(SemanticLayerError) as caught:
        AdbcAdapter(OPTIONS, profile=SNOWFLAKE_PROFILE).query("SELECT 1")
    assert caught.value.code == "MISSING_DEPENDENCY"
    assert "snowflake-adbc" in str(caught.value)
    assert "synthetic-password" not in str(caught.value)


@pytest.mark.parametrize("kind", ["snowflake_cli", "snowflake_native"])
def test_existing_snowflake_paths_keep_their_adapters(kind):
    from semantic_rails.db_parts.snowflake import SnowflakeCliAdapter, SnowflakeNativeAdapter

    package = PackageMeta(
        package_id="test",
        name="test",
        description="test",
        warehouse="snowflake",
        connection=ConnectionSpec(kind=kind, name="existing-profile"),
    )
    adapter = create_warehouse_adapter(package)
    assert isinstance(
        adapter, SnowflakeCliAdapter if kind == "snowflake_cli" else SnowflakeNativeAdapter
    )
    assert not adapter.supports_parameters
    adapter.close()


@pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata"])
def test_arrow_decimal_and_timestamp_match_typed_values_contract(monkeypatch, zone):
    pa = pytest.importorskip("pyarrow")
    amount = Decimal("123456789012345678.123456789")
    instant = datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)
    batch = pa.record_batch(
        [
            pa.array([amount, None], type=pa.decimal128(38, 9)),
            pa.array([instant, None], type=pa.timestamp("ns", tz="UTC")),
            pa.array([instant.replace(tzinfo=None), None], type=pa.timestamp("ns")),
        ],
        names=["amount", "instant", "local"],
    )
    calls = []

    class ArrowCursor(Cursor):
        def fetch_record_batch(self):
            return pa.RecordBatchReader.from_batches(batch.schema, [batch])

    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    adapter._conn = SimpleNamespace(cursor=lambda: ArrowCursor(calls), close=lambda: None)
    try:
        rows = adapter.query("SELECT amount, instant, local", limits={"time_zone": zone})
        assert type(rows[0]["amount"]) is Decimal
        assert rows[0]["amount"].as_tuple() == amount.as_tuple()
        assert rows[0]["instant"].astimezone(UTC) == instant
        assert rows[0]["instant"].utcoffset() == (
            timedelta(0) if zone == "UTC" else timedelta(hours=5, minutes=30)
        )
        assert rows[0]["local"].tzinfo is None
        wire = result_rows(rows, zone=zone)
        assert wire["rows"][0]["amount"] == str(amount)
        assert wire["rows"][0]["instant"] == (
            "2026-09-30T07:04:56.123456+00:00"
            if zone == "UTC"
            else "2026-09-30T12:34:56.123456+05:30"
        )
        assert wire["column_types"] == {
            "amount": {"type": "decimal"},
            "instant": {"type": "timestamp", "timezone": "aware"},
            "local": {"type": "timestamp", "timezone": "naive"},
        }
        assert wire["rows"][1] == {"amount": None, "instant": None, "local": None}
    finally:
        adapter.close()


@pytest.mark.parametrize("kind", ["timestamp", "timestamp_utc", "time"])
@pytest.mark.parametrize("value", [123456000, 123456789, -123456789, None])
@pytest.mark.parametrize("prepared", [False, True])
def test_nanosecond_temporals_require_exact_microseconds_without_pandas(
    monkeypatch, kind, value, prepared
):
    pa = pytest.importorskip("pyarrow")
    monkeypatch.setitem(sys.modules, "pandas", None)
    arrow_type = {
        "timestamp": pa.timestamp("ns"),
        "timestamp_utc": pa.timestamp("ns", tz="UTC"),
        "time": pa.time64("ns"),
    }[kind]
    batch = pa.record_batch([pa.array([value], type=arrow_type)], names=["instant"])
    calls = []

    class ArrowCursor(Cursor):
        def fetch_record_batch(self):
            return pa.RecordBatchReader.from_batches(batch.schema, [batch])

    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    adapter._conn = SimpleNamespace(
        cursor=lambda: ArrowCursor(calls), close=lambda: calls.append("closed")
    )
    query = (
        (lambda: adapter.query_prepared(PreparedQuery("SELECT instant")))
        if prepared
        else (lambda: adapter.query("SELECT instant"))
    )
    try:
        if value is not None and value % 1000:
            with pytest.raises(SemanticLayerError) as caught:
                query()
            assert caught.value.code == "RESULT_VALUE_UNSUPPORTED"
            assert caught.value.details == {"column": "instant", "type": str(arrow_type)}
            assert adapter._conn is None
            assert calls[-1] == "closed"
        else:
            expected = None
            if value is not None:
                expected = {
                    "time": time(0, 0, 0, 123456),
                    "timestamp": datetime(1970, 1, 1, microsecond=123456),
                    "timestamp_utc": datetime(1970, 1, 1, microsecond=123456, tzinfo=UTC),
                }[kind]
            assert query() == [{"instant": expected}]
    finally:
        adapter.close()


def test_no_overrides_preserve_inherited_timeout_and_zone(monkeypatch):
    calls = []
    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    adapter._conn = SimpleNamespace(cursor=lambda: Cursor(calls), close=lambda: None)
    adapter.query("SELECT 1 AS account$1")
    assert calls == [
        ("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION", None),
        ("SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION", None),
        ("SELECT 1 AS account$1", None),
    ]
    adapter.close()


@pytest.mark.parametrize(
    ("sql", "slots", "values"),
    [
        ("SELECT ?", (), ()),
        ("SELECT $1", (SLOT,), ("tenant",)),
        ("SELECT ?, ?", (SLOT,), ("tenant",)),
        ("SELECT '?'", (SLOT,), ("tenant",)),
        ("SELECT ? + $1", (SLOT,), ("tenant",)),
        ("SELECT ?", (SLOT,), ()),
        ("SELECT ?", (SLOT,), (True,)),
    ],
)
def test_direct_prepared_bypasses_deny_before_connection(sql, slots, values, monkeypatch):
    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query_prepared(PreparedQuery(sql, parameters=slots), parameters=values)
    assert caught.value.code == "POLICY_DENIED"


def test_plain_query_unbound_qmark_denies_before_connection(monkeypatch):
    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query("SELECT ?")
    assert caught.value.details == {"reason": "parameter_placeholder_mismatch"}


def test_all_authored_slot_types_bind_separately():
    calls = []
    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    adapter._conn = SimpleNamespace(cursor=lambda: Cursor(calls), close=lambda: None)
    prepared = PreparedQuery(
        "SELECT ?, ?, ?",
        parameters=(SLOT, ParameterSlot("number", "integer"), ParameterSlot("enabled", "boolean")),
    )
    adapter.query_prepared(prepared, parameters=("tenant' OR true --", 42, False))
    assert (prepared.sql, ("tenant' OR true --", 42, False)) in calls
    assert all("tenant" not in sql for sql, _ in calls)
    adapter.close()


@pytest.mark.parametrize(
    ("limit_ms", "option_s", "expected_s"), [(250, 9, 1), (1001, 9, 2), (0, 9, 9), (0, 0, 0)]
)
def test_immutable_qmark_binding_timeout_and_session_restore(
    monkeypatch, limit_ms, option_s, expected_s
):
    calls = []
    timers = []

    class Timer:
        def __init__(self, delay, callback):
            timers.append(delay)

        def start(self):
            pass

        def cancel(self):
            timers.append("cancelled")

        def join(self):
            timers.append("joined")

    monkeypatch.setattr("semantic_rails.db_parts.adbc.threading.Timer", Timer)
    adapter = AdbcAdapter({"statement_timeout_seconds": str(option_s)}, profile=SNOWFLAKE_PROFILE)
    adapter._conn = SimpleNamespace(
        cursor=lambda: Cursor(calls), close=lambda: calls.append("closed")
    )
    # Include quoted/commented qmarks and an escaped quote in a Snowflake literal.
    prepared = PreparedQuery(
        "SELECT ? AS physical, '?' AS \"?\", $$?$$, 'escaped\\'?' -- ?\n/* ? */",
        (("physical", "semantic"),),
        (SLOT,),
    )
    result = adapter.query_prepared(
        prepared,
        parameters=("tenant' OR true --",),
        limits={"statement_timeout_ms": limit_ms, "time_zone": "Asia/Kolkata"},
    )
    assert (prepared.sql, ("tenant' OR true --",)) in calls
    assert all("tenant" not in sql for sql, _ in calls)
    timeout_calls = [
        call for call in calls if call[0].startswith("ALTER SESSION SET STATEMENT_TIMEOUT")
    ]
    assert timeout_calls == (
        [
            (f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {expected_s}", None),
            ("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 42", None),
        ]
        if expected_s
        else []
    )
    assert calls[-1:] == [
        ("ALTER SESSION SET TIMEZONE = 'UTC'", None),
    ]
    assert result[0]["semantic"].as_tuple().exponent == -4
    assert result[0]["z"].hour == 5 and result[0]["z"].minute == 30
    assert timers == (
        [(limit_ms or option_s * 1000) / 1000, "cancelled", "joined"] if expected_s else []
    )


@pytest.mark.parametrize(
    "failure",
    [
        "SELECT ?",
        "fetch",
        "cancel",
        "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 42",
        "ALTER SESSION SET TIMEZONE = 'UTC'",
    ],
)
def test_execution_fetch_cancel_and_reset_failures_discard_and_redact(monkeypatch, failure):
    calls = []

    class Timer:
        def __init__(self, delay, callback):
            self.callback = callback

        def start(self):
            if failure == "cancel":
                self.callback()

        def cancel(self):
            calls.append("timer-cancelled")

        def join(self):
            calls.append("timer-joined")

    monkeypatch.setattr("semantic_rails.db_parts.adbc.threading.Timer", Timer)
    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    adapter._conn = SimpleNamespace(
        cursor=lambda: Cursor(calls, failure), close=lambda: calls.append("closed")
    )
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query_prepared(
            PreparedQuery("SELECT ?", parameters=(SLOT,)),
            parameters=("secret-tenant-canary",),
            limits={"statement_timeout_ms": 250, "time_zone": "Asia/Kolkata"},
        )
    assert adapter._conn is None
    assert caught.value.code == "QUERY_EXECUTION_ERROR"
    assert "driver-secret-canary" not in str(caught.value)
    assert "secret-tenant-canary" not in str(caught.value)
    assert calls.index("timer-joined") < calls.index("closed")


def test_failed_tag_setup_closes_connection(driver):
    # A tag failure is a connection-initialization failure, before reuse.
    from unittest.mock import patch

    calls = []
    conn = SimpleNamespace(
        cursor=lambda: Cursor(calls, "ALTER SESSION SET QUERY_TAG = 'tag'"),
        close=lambda: calls.append("closed"),
    )
    with patch(
        "semantic_rails.db_parts.adbc.import_driver",
        return_value=SimpleNamespace(connect=lambda **kwargs: conn),
    ):
        adapter = AdbcAdapter({**OPTIONS, "query_tag": "tag"}, profile=SNOWFLAKE_PROFILE)
        with pytest.raises(SemanticLayerError):
            adapter.query("SELECT 1")
    assert adapter._conn is None
    assert calls[-1] == "closed"


@pytest.mark.parametrize("kind", ["snowflake_native", "snowflake_adbc"])
def test_direct_adapter_cannot_bypass_package_driver_refusal(kind):
    package = PackageMeta(
        package_id="test",
        name="test",
        description="test",
        warehouse="snowflake",
        connection=ConnectionSpec(kind=kind, options={"driver_path": "/tmp/driver.so"}),
    )
    with pytest.raises(SemanticLayerError) as caught:
        create_warehouse_adapter(package)
    assert caught.value.code == "INVALID_CONFIG"


def test_live_fixture_skips_explicitly_before_connection_without_credentials(monkeypatch):
    from tests.integration.test_adbc_snowflake import TARGET, adbc

    monkeypatch.setattr(type(TARGET), "missing_env", lambda self: ("SR_SNOWFLAKE_PASSWORD",))
    monkeypatch.setattr(
        AdbcAdapter, "_connection", lambda self: pytest.fail("must skip before connection")
    )
    with pytest.raises(pytest.skip.Exception, match="requires SR_SNOWFLAKE_ACCOUNT"):
        next(adbc.__wrapped__())
