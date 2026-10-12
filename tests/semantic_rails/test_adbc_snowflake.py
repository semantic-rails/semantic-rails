"""Driver-free Snowflake profile checks; no warehouse credentials required."""

import builtins
import json
import subprocess
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

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
ENV = {"TEST_ACCOUNT": "test-account", "TEST_USER": "test-user", "TEST_PASSWORD": "password-canary"}
OPTIONS = {"account_env": "TEST_ACCOUNT", "user_env": "TEST_USER", "password_env": "TEST_PASSWORD"}
LOCATORS = [
    {"account_env": "TEST_ACCOUNT", "user_env": "TEST_USER"},
    {"account": "test-account", "user": "test-user"},
    {"account": "test-account", "user_env": "TEST_USER"},
    {"account_env": "TEST_ACCOUNT", "user": "test-user"},
]
KEY_FILE = {"private_key_file": "/not-read/key"}
PASSPHRASE_ENV = {"private_key_passphrase_env": "TEST_PASSPHRASE"}
PASSPHRASE_FILE = {"private_key_passphrase_file": "/not-read/passphrase"}
LITERAL = "literal-secret-canary"


def _without(key: str) -> dict[str, str]:
    """The password options without one of them."""
    return {name: value for name, value in OPTIONS.items() if name != key}


def _meta(kind: str = "snowflake_adbc", **connection: Any) -> PackageMeta:
    return PackageMeta(
        package_id="test",
        name="test",
        description="test",
        warehouse="snowflake",
        connection=ConnectionSpec(kind=kind, **connection),
    )


class Cursor:
    def __init__(self, calls, failure="", batch=None):
        self.calls = calls
        self.failure = failure
        self.batch = batch  # a real Arrow batch to return, when given
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
        if self.batch is not None:
            import pyarrow as pa

            return pa.RecordBatchReader.from_batches(self.batch.schema, [self.batch])
        return Reader(
            [Batch([{"physical": Decimal("123.4500"), "z": datetime(2026, 1, 1, tzinfo=UTC)}])]
        )

    def adbc_cancel(self):
        self.calls.append("cancel")
        if self.failure == "cancel":
            raise RuntimeError("driver-secret-canary")


def _connected(calls, options=None, failure="", batch=None) -> AdbcAdapter:
    """An adapter on an open fake connection whose cursors record into `calls`."""
    adapter = AdbcAdapter(options or {}, profile=SNOWFLAKE_PROFILE)
    adapter._conn = SimpleNamespace(
        cursor=lambda: Cursor(calls, failure, batch), close=lambda: calls.append("closed")
    )
    return adapter


def _timer(events, fire=False):
    """A statement-timeout timer that records its delay, cancel and join into `events`."""

    class Timer:
        def __init__(self, delay, callback):
            events.append(delay)
            self.callback = callback

        def start(self):
            if fire:
                self.callback()

        def cancel(self):
            events.append("cancelled")

        def join(self):
            events.append("joined")

    return Timer


@pytest.fixture
def driver(monkeypatch):
    calls = []
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    def connect(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(cursor=lambda: Cursor(calls), close=lambda: calls.append("closed"))

    monkeypatch.setattr(
        "semantic_rails.db_parts.adbc.import_driver",
        lambda *args, **kwargs: SimpleNamespace(connect=connect),
    )
    return calls


def _snowflake_root(tmp_path, connection):
    """A package on Snowflake whose connection block is `connection`."""
    root = _package(tmp_path / "filtered", [OWN_ORDERS])
    path = root / "package.yml"
    payload = yaml.safe_load(path.read_text())
    payload["package"].update(warehouse="snowflake", connection=connection)
    path.write_text(yaml.safe_dump(payload))
    return root


def _guided_setup(tmp_path, options, **connection):
    from semantic_rails.architect_mcp import create_architect_mcp_server
    from tests.semantic_rails.test_architect_create_project import _setup_dialog

    arguments = {
        "package_id": "snow_shop",
        "warehouse": "snowflake",
        "connection_kind": "snowflake_adbc",
        **connection,
        "connection_options": json.dumps(options),
    }
    server = create_architect_mcp_server(workspace_root=tmp_path)
    return _setup_dialog(server, "snow_shop", arguments)


# Each authentication source and each locator form, unnamed.
@pytest.mark.parametrize(
    ("locators", "auth"),
    [
        (LOCATORS[0], {"password_env": "TEST_PASSWORD"}),
        (LOCATORS[1], {"password_file": "/not-read/password"}),
        (LOCATORS[2], {"private_key_env": "TEST_KEY"}),
        (LOCATORS[3], {**KEY_FILE, **PASSPHRASE_ENV}),
        (LOCATORS[0], {**KEY_FILE, **PASSPHRASE_FILE}),
    ],
)
def test_unnamed_snowflake_adbc_loads_dispatches_and_passes_report_and_guided_setup(
    tmp_path, locators, auth
):
    from tests.semantic_rails.test_config_validation import (
        _write_minimal_snowflake_package,
        _write_yaml,
    )

    root = tmp_path / "snowflake_adbc_demo"
    _write_minimal_snowflake_package(root)
    payload = yaml.safe_load((root / "package.yml").read_text())
    options = {**locators, **auth}
    payload["package"]["connection"] = {"kind": "snowflake_adbc", "options": options}
    _write_yaml(root / "package.yml", payload)
    report, _ = parse_config_report(resolve_package_reference(path=str(root)))
    assert report["ok"] is True, report["errors"]
    adapter = create_warehouse_adapter(load_package_config(str(root)).package)
    assert isinstance(adapter, AdbcAdapter)
    assert adapter.profile == SNOWFLAKE_PROFILE
    assert (adapter.engine, adapter.connection_kind) == ("snowflake", "snowflake_adbc")
    assert adapter.supports_parameters and adapter.supports_statement_timeout
    adapter.close()
    dialog = _guided_setup(tmp_path, options)
    assert dialog["ok"] is True, dialog
    assert dialog["recommended_next_tool"] == "create_project"


# (options, connection name, the option the refusal names)
AUTH_REFUSALS = {
    "account-missing": (_without("account_env"), "", "account_env"),
    "user-missing": (_without("user_env"), "", "user_env"),
    "auth-missing": (_without("password_env"), "", "exactly one"),
    "conflicting-auth": ({**OPTIONS, "private_key_env": "TEST_KEY"}, "", "exactly one"),
    "passphrase-without-key": ({**OPTIONS, **PASSPHRASE_ENV}, "", "passphrase"),
    "passphrase-file-without-key": ({**OPTIONS, **PASSPHRASE_FILE}, "", "passphrase"),
    "literal-password": ({**OPTIONS, "password": LITERAL}, "", "password"),
    "literal-key": ({**OPTIONS, "private_key": LITERAL}, "", "private_key"),
    "literal-passphrase": ({**OPTIONS, "private_key_passphrase": LITERAL}, "", "passphrase"),
    "named-profile": (OPTIONS, "ignored-profile", "name"),
}


@pytest.mark.parametrize(
    ("options", "name", "error"), list(AUTH_REFUSALS.values()), ids=list(AUTH_REFUSALS)
)
def test_auth_structure_refused_at_load_report_and_guided_setup(tmp_path, options, name, error):
    connection = {"kind": "snowflake_adbc", "name": name, "options": options}
    root = _snowflake_root(tmp_path, connection)
    with pytest.raises(SemanticLayerError) as caught:
        load_package_config(str(root))
    assert caught.value.code == "INVALID_CONFIG"
    assert error in str(caught.value)
    report, _ = parse_config_report(resolve_package_reference(path=str(root)))
    assert report["ok"] is False
    assert any(error in entry["message"] for entry in report["errors"])
    dialog = _guided_setup(tmp_path, options, connection_name=name)
    assert dialog["ok"] is False
    assert dialog["recommended_next_tool"] == "setup_project_dialog"
    with pytest.raises(SemanticLayerError, match=error) as caught:
        create_warehouse_adapter(_meta(name=name, options=options))._snowflake_connect_options()
    assert caught.value.code == "INVALID_CONFIG"


def test_embedding_exports_snowflake_adbc_options():
    from semantic_rails import embedding
    from semantic_rails.dialects import SNOWFLAKE_ADBC_CONNECTION_OPTIONS

    assert "SNOWFLAKE_ADBC_CONNECTION_OPTIONS" in embedding.__all__
    assert embedding.SNOWFLAKE_ADBC_CONNECTION_OPTIONS is SNOWFLAKE_ADBC_CONNECTION_OPTIONS
    assert {"account", "user", "private_key_passphrase_file"} <= set(
        embedding.SNOWFLAKE_ADBC_CONNECTION_OPTIONS
    )


@pytest.mark.parametrize("auth", ["password", "private_key", "encrypted_private_key"])
def test_literal_locators_and_file_secrets_without_environment(monkeypatch, tmp_path, auth):
    for name in ("ACCOUNT", "USER", "PASSWORD", "KEY", "PASSPHRASE", "PRIVATE_KEY_PASSPHRASE"):
        monkeypatch.delenv(f"TEST_{name}", raising=False)
        monkeypatch.delenv(f"SR_SNOWFLAKE_{name}", raising=False)
    secret = tmp_path / "secret"
    secret.write_text("file-secret-canary\n")
    key_pair = auth != "password"
    options = {
        "account": "literal-account",
        "user": "literal-user",
        "private_key_file" if key_pair else "password_file": str(secret),
    }
    if auth == "encrypted_private_key":
        passphrase = tmp_path / "passphrase"
        passphrase.write_text("file-passphrase-canary\n")
        options["private_key_passphrase_file"] = str(passphrase)
    # Any environment lookup during construction or credential resolution fails.
    with monkeypatch.context() as guarded:
        guarded.setattr("semantic_rails.db_parts.common.os.environ", None)
        adapter = create_warehouse_adapter(_meta(options=options))
        assert isinstance(adapter, AdbcAdapter)
        assert adapter.profile == SNOWFLAKE_PROFILE
        connect_options = adapter._snowflake_connect_options()
        adapter.close()
    expected = {
        "adbc.snowflake.sql.account": "literal-account",
        "username": "literal-user",
        "adbc.snowflake.sql.auth_type": "auth_jwt" if key_pair else "auth_snowflake",
        "adbc.snowflake.sql.client_option.use_high_precision": "true",
        "adbc.snowflake.sql.client_option.max_timestamp_precision": "nanoseconds_error_on_overflow",
        "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_value"
        if key_pair
        else "password": ("file-secret-canary"),
    }
    if auth == "encrypted_private_key":
        expected["adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_password"] = (
            "file-passphrase-canary"
        )
    assert connect_options == expected


@pytest.fixture(scope="module")
def rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    ("passphrase", "terminator"),
    [
        (" synthetic-passphrase ", ""),
        (" synthetic-passphrase ", "\n"),
        (" synthetic-passphrase ", "\r\n"),
        (" synthetic-passphrase \n", "\n"),
        (" synthetic-passphrase \r\n", "\r\n"),
        (" synthetic-passphrase \r", ""),
        (" synthetic\r\npassphrase ", "\n"),
    ],
)
def test_passphrase_file_matches_environment_and_decrypts_pkcs8(
    monkeypatch, tmp_path, rsa_key, passphrase, terminator
):
    from cryptography.hazmat.primitives import serialization

    encrypted = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(encrypted)
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_bytes((passphrase + terminator).encode("utf-8"))
    monkeypatch.setenv("TEST_PASSPHRASE", passphrase)
    options = {"account": "test-account", "user": "test-user", "private_key_file": str(key_file)}
    resolved = []
    for source, value in (("env", "TEST_PASSPHRASE"), ("file", str(passphrase_file))):
        adapter = AdbcAdapter(
            {**options, f"private_key_passphrase_{source}": value}, profile=SNOWFLAKE_PROFILE
        )
        connect_options = adapter._snowflake_connect_options()
        adapter.close()
        prefix = "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8"
        password = connect_options[f"{prefix}_password"]
        decrypted = serialization.load_pem_private_key(
            connect_options[f"{prefix}_value"].encode("utf-8"), password=password.encode("utf-8")
        )
        assert decrypted.public_key().public_numbers() == rsa_key.public_key().public_numbers()
        resolved.append(password)
    assert resolved == [passphrase, passphrase]


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
    connection = {"kind": "snowflake_adbc", "options": dict(OPTIONS)}
    (connection if inline else connection["options"])[key] = value
    root = _snowflake_root(tmp_path, connection)
    with pytest.raises(SemanticLayerError) as caught:
        load_package_config(str(root))
    assert caught.value.code == "INVALID_CONFIG"
    assert key in str(caught.value)
    report, _ = parse_config_report(resolve_package_reference(path=str(root)))
    assert report["ok"] is False


def test_password_file_mapping(driver, tmp_path):
    path = tmp_path / "password"
    path.write_text("file-password-canary\n")
    options = {**_without("password_env"), "password_file": str(path)}
    adapter = AdbcAdapter(options, profile=SNOWFLAKE_PROFILE)
    adapter._connection()
    assert driver[0]["db_kwargs"]["password"] == "file-password-canary"
    adapter.close()


@pytest.mark.parametrize(
    ("driver_path", "precision"),
    [(None, None), ("/opt/drivers/libsnowflake.so", "true"), ("/opt/snowflake.toml", "false")],
)
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


@pytest.mark.parametrize(("source", "encrypted"), [("env", False), ("file", True)])
def test_pkcs8_key_pair_mapping_without_password(driver, monkeypatch, tmp_path, source, encrypted):
    options = _without("password_env")
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
        {"password": LITERAL},
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
    assert LITERAL not in str(caught.value)
    assert not driver


def test_missing_env_refused_before_connect(driver, monkeypatch):
    monkeypatch.delenv("TEST_PASSWORD")
    with pytest.raises(SemanticLayerError) as caught:
        AdbcAdapter(OPTIONS, profile=SNOWFLAKE_PROFILE)._connection()
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.details["missing_env"] == ["TEST_PASSWORD"]
    assert not driver


def test_missing_python_driver_names_opt_in_extra(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setitem(sys.modules, "adbc_driver_manager.dbapi", None)
    with pytest.raises(SemanticLayerError) as caught:
        AdbcAdapter(OPTIONS, profile=SNOWFLAKE_PROFILE).query("SELECT 1")
    assert caught.value.code == "MISSING_DEPENDENCY"
    assert "snowflake-adbc" in str(caught.value)
    assert "password-canary" not in str(caught.value)


@pytest.mark.parametrize("kind", ["snowflake_cli", "snowflake_native"])
def test_existing_snowflake_paths_keep_their_adapters(kind):
    from semantic_rails.db_parts.snowflake import SnowflakeCliAdapter, SnowflakeNativeAdapter

    adapter = create_warehouse_adapter(_meta(kind, name="existing-profile"))
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
    adapter = _connected([], batch=batch)
    rows = adapter.query("SELECT amount, instant, local", limits={"time_zone": zone})
    adapter.close()
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
        "2026-09-30T07:04:56.123456+00:00" if zone == "UTC" else "2026-09-30T12:34:56.123456+05:30"
    )
    assert wire["column_types"] == {
        "amount": {"type": "decimal"},
        "instant": {"type": "timestamp", "timezone": "aware"},
        "local": {"type": "timestamp", "timezone": "naive"},
    }
    assert wire["rows"][1] == {"amount": None, "instant": None, "local": None}


# Each kind converts an exact value and refuses an inexact one, through both entry points.
@pytest.mark.parametrize(
    ("kind", "value", "prepared"),
    [
        ("timestamp", 123456000, False),
        ("timestamp_utc", 123456000, True),
        ("time", 123456000, False),
        ("timestamp", 123456789, True),
        ("timestamp_utc", -123456789, False),
        ("time", 123456789, True),
        ("time", None, False),
    ],
)
def test_nanosecond_temporals_require_exact_microseconds_without_pandas(
    monkeypatch, kind, value, prepared
):
    pa = pytest.importorskip("pyarrow")
    original_import = builtins.__import__

    def without_pandas(name, *args, **kwargs):
        if name == "pandas" or name.startswith("pandas."):
            raise ModuleNotFoundError("No module named 'pandas'", name="pandas")
        return original_import(name, *args, **kwargs)

    # A None entry in sys.modules returns None to PyArrow's pandas shim;
    # an absent installation raises ModuleNotFoundError instead.
    monkeypatch.setattr(builtins, "__import__", without_pandas)
    arrow_type = {
        "timestamp": pa.timestamp("ns"),
        "timestamp_utc": pa.timestamp("ns", tz="UTC"),
        "time": pa.time64("ns"),
    }[kind]
    calls = []
    batch = pa.record_batch([pa.array([value], type=arrow_type)], names=["instant"])
    adapter = _connected(calls, batch=batch)
    query = (
        (lambda: adapter.query_prepared(PreparedQuery("SELECT instant")))
        if prepared
        else (lambda: adapter.query("SELECT instant"))
    )
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
        adapter.close()


def test_without_pandas_utc_conversion_in_fresh_process():
    pytest.importorskip("pyarrow")
    # PyArrow caches pandas availability. Start fresh so earlier Arrow tests
    # cannot hide a broken simulation of an absent pandas installation.
    script = """
from pytest import MonkeyPatch
from tests.semantic_rails.test_adbc_snowflake import (
    test_nanosecond_temporals_require_exact_microseconds_without_pandas as check,
)
for prepared in (False, True):
    for value in (123456000, None):
        with MonkeyPatch.context() as patch:
            check(patch, "timestamp_utc", value, prepared)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_overrides_preserve_inherited_timeout_and_zone():
    calls = []
    _connected(calls).query("SELECT 1 AS account$1")
    assert calls == [
        ("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION", None),
        ("SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION", None),
        ("SELECT 1 AS account$1", None),
    ]


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
        ("SELECT ?", None, None),  # a plain query with an unbound placeholder
    ],
)
def test_unbound_placeholders_deny_before_connection(sql, slots, values, monkeypatch):
    adapter = AdbcAdapter(profile=SNOWFLAKE_PROFILE)
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        if slots is None:
            adapter.query(sql)
        else:
            adapter.query_prepared(PreparedQuery(sql, parameters=slots), parameters=values)
    assert caught.value.code == "POLICY_DENIED"
    if slots is None:
        assert caught.value.details == {"reason": "parameter_placeholder_mismatch"}


def test_all_authored_slot_types_bind_separately():
    calls = []
    prepared = PreparedQuery(
        "SELECT ?, ?, ?",
        parameters=(SLOT, ParameterSlot("number", "integer"), ParameterSlot("enabled", "boolean")),
    )
    _connected(calls).query_prepared(prepared, parameters=("tenant' OR true --", 42, False))
    assert (prepared.sql, ("tenant' OR true --", 42, False)) in calls
    assert all("tenant" not in sql for sql, _ in calls)


@pytest.mark.parametrize(
    ("limit_ms", "option_s", "expected_s"), [(250, 9, 1), (1001, 9, 2), (0, 9, 9), (0, 0, 0)]
)
def test_immutable_qmark_binding_timeout_and_session_restore(
    monkeypatch, limit_ms, option_s, expected_s
):
    calls = []
    timers = []
    monkeypatch.setattr("semantic_rails.db_parts.adbc.threading.Timer", _timer(timers))
    adapter = _connected(calls, {"statement_timeout_seconds": str(option_s)})
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
    assert calls[-1:] == [("ALTER SESSION SET TIMEZONE = 'UTC'", None)]
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
    timer = _timer(calls, fire=failure == "cancel")
    monkeypatch.setattr("semantic_rails.db_parts.adbc.threading.Timer", timer)
    adapter = _connected(calls, failure=failure)
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
    assert calls.index("joined") < calls.index("closed")


def test_failed_tag_setup_closes_connection(driver, monkeypatch):
    # A tag failure is a connection-initialization failure, before reuse.
    calls = []
    conn = SimpleNamespace(
        cursor=lambda: Cursor(calls, "ALTER SESSION SET QUERY_TAG = 'tag'"),
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(
        "semantic_rails.db_parts.adbc.import_driver",
        lambda *args, **kwargs: SimpleNamespace(connect=lambda **kwargs: conn),
    )
    adapter = AdbcAdapter({**OPTIONS, "query_tag": "tag"}, profile=SNOWFLAKE_PROFILE)
    with pytest.raises(SemanticLayerError):
        adapter.query("SELECT 1")
    assert adapter._conn is None
    assert calls[-1] == "closed"


@pytest.mark.parametrize("kind", ["snowflake_native", "snowflake_adbc"])
def test_direct_adapter_cannot_bypass_package_driver_refusal(kind):
    with pytest.raises(SemanticLayerError) as caught:
        create_warehouse_adapter(_meta(kind, options={"driver_path": "/tmp/driver.so"}))
    assert caught.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("snowflake_adbc", "requires SR_SNOWFLAKE_ACCOUNT"),
        (None, "requires SR_SNOWFLAKE_CONNECTION_KIND=snowflake_adbc"),
        ("snowflake_native", "requires SR_SNOWFLAKE_CONNECTION_KIND=snowflake_adbc"),
    ],
)
def test_live_fixture_skips_before_connection_without_opt_in_or_credentials(
    monkeypatch, kind, reason
):
    from tests.integration.test_adbc_snowflake import TARGET, adbc

    if kind is None:
        monkeypatch.delenv("SR_SNOWFLAKE_CONNECTION_KIND", raising=False)
    else:
        monkeypatch.setenv("SR_SNOWFLAKE_CONNECTION_KIND", kind)
    monkeypatch.setattr(type(TARGET), "missing_env", lambda self: ("SR_SNOWFLAKE_PASSWORD",))
    monkeypatch.setattr(
        AdbcAdapter, "_connection", lambda self: pytest.fail("must skip before connection")
    )
    with pytest.raises(pytest.skip.Exception, match=reason):
        next(adbc.__wrapped__())


def test_live_setup_refuses_unavailable_driver(monkeypatch):
    from tests.integration.test_adbc_snowflake import setup_adbc

    monkeypatch.setenv("SR_SNOWFLAKE_CONNECTION_KIND", "snowflake_adbc")
    for key in ("ACCOUNT", "USER", "PASSWORD"):
        monkeypatch.setenv(f"SR_SNOWFLAKE_{key}", f"synthetic-{key.lower()}")
    monkeypatch.setenv("SR_SNOWFLAKE_ADBC_DRIVER_PATH", "/does-not-exist/snowflake-driver.so")
    with pytest.raises(SemanticLayerError) as caught:
        setup_adbc()
    assert caught.value.code in {"MISSING_DEPENDENCY", "QUERY_EXECUTION_ERROR"}


@pytest.mark.parametrize("correct", [True, False])
def test_live_setup_asserts_timestamp_and_closes_on_mismatch(monkeypatch, correct):
    from tests.integration.test_adbc_snowflake import TARGET, setup_adbc

    monkeypatch.setenv("SR_SNOWFLAKE_CONNECTION_KIND", "snowflake_adbc")
    monkeypatch.setattr(type(TARGET), "missing_env", lambda self: ())
    calls = []

    def query(self, sql):
        calls.append(sql)
        return [{"INSTANT": datetime(2026, 1, 1, 12, 34, 56, 123456) if correct else None}]

    monkeypatch.setattr(AdbcAdapter, "query", query)
    monkeypatch.setattr(AdbcAdapter, "close", lambda self: calls.append("closed"))
    if correct:
        adapter = setup_adbc()
        assert len(calls) == 1
        adapter.close()
    else:
        with pytest.raises(AssertionError):
            setup_adbc()
    assert len(calls) == 2 and calls[-1] == "closed"
