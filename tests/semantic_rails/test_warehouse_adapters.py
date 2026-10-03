from __future__ import annotations

import sys
import threading
import time
import types
from types import SimpleNamespace

import duckdb
import pytest

from semantic_rails.db import (
    DuckDBAdapter,
    SnowflakeCliAdapter,
    SnowflakeNativeAdapter,
    build_snowflake_cli_command,
    create_warehouse_adapter,
)
from semantic_rails.db_parts.common import rows_from_cursor
from semantic_rails.dialects import (
    DuckDbDialect,
    SnowflakeDialect,
    supported_warehouses,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.schema import ConnectionSpec, PackageMeta


def test_snowflake_cli_adapter_parses_json_ext_rows(monkeypatch: pytest.MonkeyPatch):
    commands = []

    def _fake_run(*args, **kwargs):
        commands.append(args[0])
        return SimpleNamespace(returncode=0, stdout='[{"ONE": 1, "TWO": "x"}]\n', stderr="")

    monkeypatch.setattr(
        "semantic_rails.db.subprocess.run",
        _fake_run,
    )

    rows = SnowflakeCliAdapter("semantic_views_trial").query("select 1")

    assert rows == [{"ONE": 1, "TWO": "x"}]
    assert commands == [
        ["snow", "sql", "-c", "semantic_views_trial", "--format", "JSON_EXT", "-q", "select 1"]
    ]


def test_dbapi_cursor_fetch_is_bounded_and_reports_truncation():
    class FakeCursor:
        description = [("value",)]

        def __init__(self):
            self.requested = 0

        def fetchmany(self, size):
            self.requested = size
            return [(1,), (2,), (3,)]

        def fetchall(self):
            raise AssertionError("bounded reads must not call fetchall")

    cursor = FakeCursor()
    rows = rows_from_cursor(cursor, limits={"max_rows": 2})

    assert cursor.requested == 3
    assert rows == [{"value": 1}, {"value": 2}]
    assert rows.truncated is True


SLOW_DUCKDB_QUERY = "SELECT count(*) FROM range(1000000000000) a"


def _duckdb_file(tmp_path) -> str:
    path = str(tmp_path / "timeout.duckdb")
    duckdb.connect(path).close()
    return path


def _interrupt_in_chain(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, duckdb.InterruptException):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _timed_query(adapter: DuckDBAdapter, timeout_ms: int) -> tuple[BaseException | None, float]:
    started = time.monotonic()
    try:
        adapter.query(SLOW_DUCKDB_QUERY, limits={"statement_timeout_ms": timeout_ms})
    except BaseException as error:  # noqa: BLE001 — handed back to the asserting thread
        return error, time.monotonic() - started
    return None, time.monotonic() - started


@pytest.mark.timeout(30)
def test_duckdb_statement_timeout_stops_the_running_query(tmp_path):
    adapter = DuckDBAdapter(_duckdb_file(tmp_path))
    try:
        error, elapsed = _timed_query(adapter, 200)
    finally:
        adapter.close()

    assert isinstance(error, SemanticLayerError)
    assert error.code == "QUERY_EXECUTION_ERROR"
    assert _interrupt_in_chain(error)
    assert elapsed < 10


@pytest.mark.timeout(30)
def test_duckdb_statement_timeout_only_stops_its_own_query(tmp_path):
    adapter = DuckDBAdapter(_duckdb_file(tmp_path))
    results: dict[int, tuple[BaseException | None, float]] = {}

    def run(timeout_ms: int) -> None:
        results[timeout_ms] = _timed_query(adapter, timeout_ms)

    threads = [threading.Thread(target=run, args=(ms,)) for ms in (5_000, 200)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        adapter.close()

    short_error, short_elapsed = results[200]
    long_error, long_elapsed = results[5_000]
    assert _interrupt_in_chain(short_error)
    assert short_elapsed < 4
    # The short query's watchdog left the long one running until its own timeout.
    assert _interrupt_in_chain(long_error)
    assert 5 <= long_elapsed < 15


def test_duckdb_query_without_timeout_starts_no_watchdog(tmp_path, monkeypatch):
    import semantic_rails.db as db_module

    def no_timer(*_args, **_kwargs):
        raise AssertionError("a query without statement_timeout_ms must not start a timer")

    monkeypatch.setattr(db_module.threading, "Timer", no_timer)
    adapter = DuckDBAdapter(_duckdb_file(tmp_path))
    try:
        assert adapter.query("SELECT 42 AS n") == [{"n": 42}]
    finally:
        adapter.close()


def test_duckdb_timeout_interrupts_a_cursor_made_after_it_fired():
    cursor_interrupted = threading.Event()

    class FakeConnection:
        def interrupt(self):
            raise AssertionError("the timeout must interrupt the statement's cursor")

    class FakeCursor:
        def interrupt(self):
            cursor_interrupted.set()

    class FakeDatabase:
        conn = FakeConnection()

        def query(self, _sql, _params=None, *, max_rows=None, on_cursor=None):
            time.sleep(0.1)  # the 10 ms timeout fires before the cursor exists
            on_cursor(FakeCursor())
            assert cursor_interrupted.wait(timeout=5)
            return []

    adapter = DuckDBAdapter.__new__(DuckDBAdapter)
    adapter._db = FakeDatabase()

    assert adapter.query("select slow", limits={"statement_timeout_ms": 10}) == []
    assert cursor_interrupted.is_set()


def test_duckdb_watchdog_uses_true_millisecond_interval(monkeypatch: pytest.MonkeyPatch):
    import semantic_rails.db as db_module

    intervals: list[float] = []

    class FakeTimer:
        daemon = False

        def __init__(self, interval, callback):
            intervals.append(interval)

        def start(self):
            return None

        def cancel(self):
            return None

        def join(self):
            return None

    class FakeDatabase:
        def query(self, _sql, _params=None, *, max_rows=None, on_cursor=None):
            return []

    monkeypatch.setattr(db_module.threading, "Timer", FakeTimer)
    adapter = DuckDBAdapter.__new__(DuckDBAdapter)
    adapter._db = FakeDatabase()

    assert adapter.query("select slow", limits={"statement_timeout_ms": 25}) == []
    assert intervals == [0.025]


def test_snowflake_cli_adapter_casts_nullif_ratio_guards_to_double(
    monkeypatch: pytest.MonkeyPatch,
):
    # Snowflake NUMBER/NUMBER division reduces result scale (~6 digits
    # live), so both Snowflake adapters apply the shared DOUBLE-cast
    # compat pass to the compiler's ratio guard before executing.
    commands = []

    def _fake_run(*args, **kwargs):
        commands.append(args[0])
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("semantic_rails.db.subprocess.run", _fake_run)

    SnowflakeCliAdapter("semantic_views_trial").query("SELECT a / NULLIF(b, 0) AS r FROM t")

    sent_sql = commands[0][-1]
    assert sent_sql == "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) AS r FROM t"


def test_snowflake_cli_adapter_maps_subprocess_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "semantic_rails.db.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="connection failed"
        ),
    )

    with pytest.raises(SemanticLayerError) as exc:
        SnowflakeCliAdapter("semantic_views_trial").query("select 1")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    assert exc.value.details["engine"] == "snowflake"
    # Adapter-level errors must NOT leak raw SQL — it is redacted by
    # default and only re-attached at the runtime layer when the caller
    # opts in and has the `debug` role.
    assert "sql" not in exc.value.details
    assert exc.value.details.get("sql_redacted") is True


def test_create_warehouse_adapter_selects_snowflake_cli():
    package = PackageMeta(
        package_id="snowflake_demo",
        name="snowflake_demo",
        description="snowflake demo",
        warehouse="snowflake",
        connection=ConnectionSpec(
            kind="snowflake_cli",
            name="semantic_views_trial",
            options={
                "database": "SNOWFLAKE_SAMPLE_DATA",
                "schema": "TPCH_SF1",
                "warehouse": "COMPUTE_WH",
                "role": "ANALYST",
            },
        ),
    )

    adapter = create_warehouse_adapter(package)

    assert isinstance(adapter, SnowflakeCliAdapter)
    assert adapter.connection_name == "semantic_views_trial"
    assert adapter.options == {
        "database": "SNOWFLAKE_SAMPLE_DATA",
        "schema": "TPCH_SF1",
        "warehouse": "COMPUTE_WH",
        "role": "ANALYST",
    }


def test_create_warehouse_adapter_selects_snowflake_native(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SNOW_ACCOUNT", "acct")
    monkeypatch.setenv("SNOW_USER", "svc_user")
    monkeypatch.setenv("SNOW_PASSWORD", "secret")
    package = PackageMeta(
        package_id="snowflake_demo",
        name="snowflake_demo",
        description="snowflake demo",
        warehouse="snowflake",
        connection=ConnectionSpec(
            kind="snowflake_native",
            name="prod_native",
            options={
                "account_env": "SNOW_ACCOUNT",
                "user_env": "SNOW_USER",
                "password_env": "SNOW_PASSWORD",
                "database": "ANALYTICS",
                "schema": "CORE",
                "warehouse": "COMPUTE_WH",
                "role": "ANALYST",
                "query_tag": "semantic-rails-test",
                "statement_timeout_seconds": "30",
            },
        ),
    )

    adapter = create_warehouse_adapter(package)

    assert isinstance(adapter, SnowflakeNativeAdapter)
    kwargs = adapter._connect_kwargs()
    assert "connection_name" not in kwargs
    assert kwargs["account"] == "acct"
    assert kwargs["user"] == "svc_user"
    assert kwargs["password"] == "secret"
    assert kwargs["database"] == "ANALYTICS"
    assert kwargs["session_parameters"] == {
        "QUERY_TAG": "semantic-rails-test",
        "STATEMENT_TIMEOUT_IN_SECONDS": 30,
    }


def test_snowflake_native_named_profile_keeps_connection_name_for_profile_mode():
    adapter = SnowflakeNativeAdapter(
        "prod_native",
        options={
            "database": "ANALYTICS",
            "schema": "CORE",
            "warehouse": "COMPUTE_WH",
            "role": "ANALYST",
        },
    )

    kwargs = adapter._connect_kwargs()

    assert kwargs["connection_name"] == "prod_native"
    assert kwargs["database"] == "ANALYTICS"
    assert kwargs["schema"] == "CORE"
    assert kwargs["warehouse"] == "COMPUTE_WH"
    assert kwargs["role"] == "ANALYST"


def test_create_warehouse_adapter_selects_snowflake_native_direct_connect(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SNOW_ACCOUNT", "acct")
    monkeypatch.setenv("SNOW_USER", "svc_user")
    monkeypatch.setenv("SNOW_PASSWORD", "secret")
    package = PackageMeta(
        package_id="snowflake_demo",
        name="snowflake_demo",
        description="snowflake demo",
        warehouse="snowflake",
        connection=ConnectionSpec(
            kind="snowflake_native",
            options={
                "account_env": "SNOW_ACCOUNT",
                "user_env": "SNOW_USER",
                "password_env": "SNOW_PASSWORD",
                "database": "ANALYTICS",
            },
        ),
    )

    adapter = create_warehouse_adapter(package)

    assert isinstance(adapter, SnowflakeNativeAdapter)
    kwargs = adapter._connect_kwargs()
    assert "connection_name" not in kwargs
    assert kwargs["account"] == "acct"
    assert kwargs["user"] == "svc_user"
    assert kwargs["password"] == "secret"
    assert kwargs["database"] == "ANALYTICS"


@pytest.mark.parametrize(
    ("timeouts", "connect_timeout", "read_timeout", "statement_timeout"),
    [
        ({}, 10, 65, None),
        (
            {
                "connect_timeout_seconds": "7",
                "read_timeout_seconds": "45",
                "statement_timeout_seconds": "40",
            },
            7,
            45,
            40,
        ),
    ],
)
def test_snowflake_native_adapter_queries_with_optional_connector(
    monkeypatch: pytest.MonkeyPatch, timeouts, connect_timeout, read_timeout, statement_timeout
):
    captured = {}

    class FakeCursor:
        description = [("ONE",), ("TWO",)]

        def execute(self, sql):
            captured["sql"] = sql

        def fetchall(self):
            return [(1, "x")]

        def close(self):
            captured["closed"] = True

    class FakeConnection:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["connection_closed"] = True

    connector_module = types.ModuleType("snowflake.connector")
    connector_module.connect = lambda **kwargs: FakeConnection(**kwargs)
    snowflake_module = types.ModuleType("snowflake")
    snowflake_module.connector = connector_module
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_module)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector_module)
    monkeypatch.setenv("SNOW_ACCOUNT", "acct")
    monkeypatch.setenv("SNOW_TOKEN", "oauth-token")
    monkeypatch.setenv("SNOW_USER", "example")

    adapter = SnowflakeNativeAdapter(
        options={
            "account_env": "SNOW_ACCOUNT",
            "user_env": "SNOW_USER",
            "authenticator": "oauth",
            "token_env": "SNOW_TOKEN",
            **timeouts,
        },
    )
    rows = adapter.query("select 1")
    adapter.close()

    assert rows == [{"ONE": 1, "TWO": "x"}]
    assert captured["sql"] == "select 1"
    assert captured["kwargs"]["account"] == "acct"
    assert "connection_name" not in captured["kwargs"]
    assert captured["kwargs"]["authenticator"] == "oauth"
    assert captured["kwargs"]["token"] == "oauth-token"
    assert captured["kwargs"]["login_timeout"] == connect_timeout
    assert captured["kwargs"]["network_timeout"] == read_timeout
    assert captured["kwargs"]["socket_timeout"] == read_timeout
    if statement_timeout is None:
        assert "session_parameters" not in captured["kwargs"]
    else:
        assert captured["kwargs"]["session_parameters"] == {
            "STATEMENT_TIMEOUT_IN_SECONDS": statement_timeout
        }
    assert captured["closed"] is True
    assert captured["connection_closed"] is True


def test_snowflake_native_direct_connect_externalbrowser_omits_connection_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SNOW_ACCOUNT", "acct")
    monkeypatch.setenv("SNOW_USER", "svc_user")
    adapter = SnowflakeNativeAdapter(
        "custom_name",
        options={
            "account_env": "SNOW_ACCOUNT",
            "user_env": "SNOW_USER",
            "authenticator": "externalbrowser",
        },
    )

    kwargs = adapter._connect_kwargs()

    assert "connection_name" not in kwargs
    assert kwargs["account"] == "acct"
    assert kwargs["user"] == "svc_user"
    assert kwargs["authenticator"] == "externalbrowser"


def test_snowflake_native_direct_connect_reports_missing_env_without_secret_value(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SNOW_ACCOUNT", "acct")
    monkeypatch.setenv("SNOW_PASSWORD", "super-secret")
    adapter = SnowflakeNativeAdapter(
        options={
            "account_env": "SNOW_ACCOUNT",
            "user_env": "SNOW_USER",
            "password_env": "SNOW_PASSWORD",
        },
    )

    with pytest.raises(SemanticLayerError) as exc:
        adapter._connect_kwargs()

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == ["SNOW_USER"]
    assert "super-secret" not in str(exc.value)
    assert "super-secret" not in repr(exc.value.details)


def test_snowflake_native_adapter_rejects_incomplete_direct_connect_options():
    with pytest.raises(SemanticLayerError) as exc:
        SnowflakeNativeAdapter(options={"account_env": "SNOW_ACCOUNT", "user_env": "SNOW_USER"})

    assert exc.value.code == "INVALID_CONFIG"
    assert "direct connection" in str(exc.value)


def test_snowflake_native_accepts_public_in_memory_credential_provider():
    class BoundCredentialProvider:
        def __init__(self):
            self.cleared = False
            self.calls = []
            self.values = {
                "account": "acct",
                "user": "svc_user",
                "token": "oauth-token",
                "private_key_passphrase": "key-passphrase",
            }

        def credentials_for(self, **identity):
            self.calls.append(identity)
            return dict(self.values)

        def clear(self):
            self.values.clear()
            self.cleared = True

    provider = BoundCredentialProvider()
    adapter = SnowflakeNativeAdapter(
        "tenant-connection",
        options={
            "database": "ANALYTICS",
            "schema": "CORE",
            "authenticator": "oauth",
            "query_tag": "hosted-query",
        },
        credential_provider=provider,
    )

    kwargs = adapter._connect_kwargs()

    assert provider.calls == [
        {
            "warehouse": "snowflake",
            "connection_kind": "snowflake_native",
            "connection_name": "tenant-connection",
        }
    ]
    assert "connection_name" not in kwargs
    assert kwargs["account"] == "acct"
    assert kwargs["user"] == "svc_user"
    assert kwargs["token"] == "oauth-token"
    assert kwargs["private_key_file_pwd"] == "key-passphrase"
    assert kwargs["database"] == "ANALYTICS"
    assert kwargs["schema"] == "CORE"
    assert kwargs["authenticator"] == "oauth"
    assert kwargs["session_parameters"]["QUERY_TAG"] == "hosted-query"

    adapter.close()

    assert provider.cleared is True
    assert adapter.credential_provider is None


def test_snowflake_credential_provider_rejects_unknown_keys_without_values():
    class BadCredentialProvider:
        def credentials_for(self, **_identity):
            return {
                "account": "acct",
                "user": "svc_user",
                "password": "super-secret",
                "unsupported_secret": "do-not-leak",
            }

    adapter = SnowflakeNativeAdapter(credential_provider=BadCredentialProvider())
    with pytest.raises(SemanticLayerError) as exc:
        adapter._connect_kwargs()

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["unsupported_keys"] == ["unsupported_secret"]
    assert "super-secret" not in str(exc.value)
    assert "do-not-leak" not in str(exc.value)


def test_snowflake_cli_command_includes_standard_connection_options():
    cmd = build_snowflake_cli_command(
        "semantic_views_trial",
        "select 1",
        {
            "database": "SNOWFLAKE_SAMPLE_DATA",
            "schema": "TPCH_SF1",
            "warehouse": "COMPUTE_WH",
            "role": "ANALYST",
        },
    )

    assert cmd == [
        "snow",
        "sql",
        "-c",
        "semantic_views_trial",
        "--database",
        "SNOWFLAKE_SAMPLE_DATA",
        "--schema",
        "TPCH_SF1",
        "--warehouse",
        "COMPUTE_WH",
        "--role",
        "ANALYST",
        "--format",
        "JSON_EXT",
        "-q",
        "select 1",
    ]


def test_snowflake_cli_adapter_rejects_unsupported_options():
    with pytest.raises(SemanticLayerError) as exc:
        SnowflakeCliAdapter("semantic_views_trial", options={"authenticator": "externalbrowser"})

    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'authenticator'" in str(exc.value)


def test_warehouse_connector_registry_exposes_first_class_duckdb_and_snowflake():
    assert supported_warehouses() == (
        "athena",
        "bigquery",
        "clickhouse",
        "databricks",
        "duckdb",
        "ducklake",
        "motherduck",
        "postgres",
        "snowflake",
    )

    duckdb = warehouse_connector("duckdb")
    snowflake = warehouse_connector("snowflake")

    assert duckdb is not None
    assert isinstance(duckdb.dialect, DuckDbDialect)
    assert duckdb.requires_default_db is True
    assert duckdb.requires_seed is True

    assert snowflake is not None
    assert isinstance(snowflake.dialect, SnowflakeDialect)
    assert snowflake.connection_kinds == ("snowflake_cli", "snowflake_native", "snowflake_adbc")
    assert "database" in snowflake.connection_options
    assert "account_env" in snowflake.connection_options
    assert "query_tag" in snowflake.connection_options
    assert snowflake.requires_connection_name is True


def test_every_registered_connector_names_an_adapter_entry_point():
    # Registry-driven factory contract: every supported warehouse must
    # carry a resolvable "module:callable" adapter entry point so
    # create_warehouse_adapter never needs per-warehouse branches.
    import importlib

    for name in supported_warehouses():
        connector = warehouse_connector(name)
        assert connector is not None
        assert connector.adapter, f"{name} connector has no adapter entry point"
        module_name, _, attr = connector.adapter.partition(":")
        factory = getattr(importlib.import_module(module_name), attr, None)
        assert callable(factory), f"{name} adapter entry point {connector.adapter} not callable"


@pytest.mark.parametrize("timeout", [None, "0", "30"])
def test_snowflake_server_timeout_is_opt_in(monkeypatch, timeout):
    monkeypatch.setenv("SR_TEST_ACCOUNT", "example")
    monkeypatch.setenv("SR_TEST_USER", "example")
    options = {
        "account_env": "SR_TEST_ACCOUNT",
        "user_env": "SR_TEST_USER",
        "authenticator": "externalbrowser",
    }
    if timeout is not None:
        options["statement_timeout_seconds"] = timeout
    kwargs = SnowflakeNativeAdapter(options=options)._connect_kwargs()
    if timeout is None:
        assert "session_parameters" not in kwargs
    else:
        assert kwargs["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": int(timeout)}


@pytest.mark.parametrize(
    "tag", ["ordinary-tag", "ops\\", "a\\', statement_timeout_in_seconds = 0 --"]
)
@pytest.mark.parametrize("entry_point", ["kwargs", "connection", "query", "prepared"])
def test_snowflake_named_profile_refuses_authored_tag_before_connect(monkeypatch, tag, entry_point):
    from unittest.mock import Mock

    from semantic_rails.sql_preparation import prepare_query

    connection = Mock()
    connect = Mock(return_value=connection)
    connector_module = types.ModuleType("snowflake.connector")
    connector_module.connect = connect
    snowflake_module = types.ModuleType("snowflake")
    snowflake_module.connector = connector_module
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_module)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector_module)
    adapter = SnowflakeNativeAdapter("analytics", options={"query_tag": tag})
    with pytest.raises(SemanticLayerError) as exc:
        if entry_point == "kwargs":
            adapter._connect_kwargs()
        elif entry_point == "connection":
            adapter._connection()
        elif entry_point == "query":
            adapter.query("select 1")
        else:
            adapter.query_prepared(prepare_query("select 1", "snowflake"))
    assert exc.value.code == "INVALID_CONFIG"
    assert "named profile" in str(exc.value)
    assert tag not in str(exc.value)
    assert tag not in repr(exc.value.details)
    connect.assert_not_called()
    connection.cursor.assert_not_called()
    connection.cursor.return_value.execute.assert_not_called()
    assert adapter._conn is None


@pytest.mark.parametrize(
    "tag", ["ordinary-tag", "ops\\", "a\\', statement_timeout_in_seconds = 0 --"]
)
def test_snowflake_direct_connection_passes_tag_as_session_parameter(monkeypatch, tag):
    from unittest.mock import Mock

    connect = Mock()
    connector_module = types.ModuleType("snowflake.connector")
    connector_module.connect = connect
    snowflake_module = types.ModuleType("snowflake")
    snowflake_module.connector = connector_module
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_module)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector_module)
    monkeypatch.setenv("SR_TEST_ACCOUNT", "example")
    monkeypatch.setenv("SR_TEST_USER", "example")
    adapter = SnowflakeNativeAdapter(
        "analytics",
        options={
            "account_env": "SR_TEST_ACCOUNT",
            "user_env": "SR_TEST_USER",
            "authenticator": "externalbrowser",
            "query_tag": tag,
        },
    )
    adapter._connection()
    assert "connection_name" not in connect.call_args.kwargs
    assert connect.call_args.kwargs["session_parameters"] == {"QUERY_TAG": tag}
    connect.return_value.cursor.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"statement_timeout_seconds": "0"},
        {
            "connect_timeout_seconds": "7",
            "read_timeout_seconds": "45",
            "statement_timeout_seconds": "20",
        },
    ],
)
def test_snowflake_named_profile_preserves_inherited_session_and_login(monkeypatch, options):
    from unittest.mock import Mock

    pytest.importorskip("snowflake.connector")
    from snowflake.connector.config_manager import CONFIG_MANAGER
    from snowflake.connector.connection import SnowflakeConnection

    profile = {
        "account": "example",
        "login_timeout": 60,
        "network_timeout": 90,
        "socket_timeout": 90,
        "session_parameters": {
            "TIMEZONE": "America/New_York",
            "QUERY_TAG": "bi",
            "STATEMENT_TIMEOUT_IN_SECONDS": 30,
        },
    }
    import tomlkit

    monkeypatch.setattr(
        CONFIG_MANAGER, "conf_file_cache", tomlkit.item({"connections": {"analytics": profile}})
    )
    monkeypatch.setattr(CONFIG_MANAGER, "read_config", lambda **kwargs: None)
    monkeypatch.setattr(
        SnowflakeConnection, "connect", lambda self, **kwargs: setattr(self, "received", kwargs)
    )
    real = SnowflakeConnection
    cursor = Mock()
    conn = real(
        connection_name="analytics",
        **{
            k: v
            for k, v in SnowflakeNativeAdapter("analytics", options=options)
            ._connect_kwargs()
            .items()
            if k != "connection_name"
        },
    )
    assert conn.received["session_parameters"] == profile["session_parameters"]
    assert conn.received["login_timeout"] == (7 if "connect_timeout_seconds" in options else 60)
    conn.cursor = lambda: cursor
    monkeypatch.setattr("snowflake.connector.connect", lambda **kwargs: conn)
    adapter = SnowflakeNativeAdapter("analytics", options=options)
    adapter._connection()
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    if "statement_timeout_seconds" in options:
        assert statements == [
            f"alter session set statement_timeout_in_seconds = {options['statement_timeout_seconds']}"
        ]
    else:
        assert statements == []
    assert conn.received["session_parameters"]["TIMEZONE"] == "America/New_York"
    assert conn.received["session_parameters"]["QUERY_TAG"] == "bi"


@pytest.mark.parametrize("failure", ["cursor", "execute", "cursor_close"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_snowflake_drops_named_profile_connection_if_timeout_setup_fails(
    monkeypatch, failure, close_fails
):
    from unittest.mock import Mock

    failed, healthy = Mock(), Mock()
    target = {
        "cursor": failed.cursor,
        "execute": failed.cursor.return_value.execute,
        "cursor_close": failed.cursor.return_value.close,
    }[failure]
    target.side_effect = RuntimeError("timeout setup failed")
    if close_fails:
        failed.close.side_effect = RuntimeError("close failed")
    connect = Mock(side_effect=[failed, healthy])
    connector_module = types.ModuleType("snowflake.connector")
    connector_module.connect = connect
    snowflake_module = types.ModuleType("snowflake")
    snowflake_module.connector = connector_module
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_module)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector_module)
    adapter = SnowflakeNativeAdapter("analytics", options={"statement_timeout_seconds": "20"})

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")
    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    assert adapter._conn is None
    failed.close.assert_called_once()
    if failure == "cursor":
        failed.cursor.return_value.execute.assert_not_called()
    assert not any(
        call.args[0] == "select 1" for call in failed.cursor.return_value.execute.call_args_list
    )
    # A later request must connect again and apply the deadline before caching.
    assert adapter._connection() is healthy
    assert connect.call_count == 2
    healthy.cursor.return_value.execute.assert_called_once_with(
        "alter session set statement_timeout_in_seconds = 20"
    )


@pytest.mark.parametrize("prepared", [False, True])
def test_snowflake_long_request_wait_and_inherited_timeout_restored(prepared):
    from semantic_rails.sql_preparation import prepare_query

    connection = SimpleNamespace(_network_timeout=65, _socket_timeout=65)
    statements = []

    class Cursor:
        description = [("value",)]

        def execute(self, sql):
            statements.append(sql)
            assert connection._network_timeout == 125
            assert connection._socket_timeout == 125

        def fetchone(self):
            return ("STATEMENT_TIMEOUT_IN_SECONDS", "30")

        def fetchall(self):
            return [(1,)]

        def close(self):
            pass

    connection.cursor = Cursor
    adapter = SnowflakeNativeAdapter("analytics")
    adapter._conn = connection
    if prepared:
        rows = adapter.query_prepared(
            prepare_query("select 1", "snowflake"), limits={"statement_timeout_ms": 120000}
        )
    else:
        rows = adapter.query("select 1", limits={"statement_timeout_ms": 120000})
    assert rows == [{"value": 1}]
    assert statements == [
        "show parameters like 'STATEMENT_TIMEOUT_IN_SECONDS' in session",
        "alter session set statement_timeout_in_seconds = 120",
        "select 1",
        "alter session set statement_timeout_in_seconds = 30",
    ]
    assert connection._network_timeout == connection._socket_timeout == 65


def test_snowflake_refuses_request_if_inherited_timeout_cannot_be_read():
    from unittest.mock import Mock

    cursor = Mock()
    cursor.fetchone.return_value = None
    connection = SimpleNamespace(cursor=lambda: cursor, _network_timeout=65, _socket_timeout=65)
    adapter = SnowflakeNativeAdapter("analytics")
    adapter._conn = connection
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1", limits={"statement_timeout_ms": 120000})
    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    assert cursor.execute.call_count == 1
    assert connection._network_timeout == connection._socket_timeout == 65


@pytest.mark.parametrize("close_fails", [False, True])
def test_snowflake_drops_connection_if_inherited_deadline_restore_fails(close_fails):
    from unittest.mock import Mock

    cursor = Mock()
    cursor.description = [("value",)]
    cursor.fetchone.return_value = ("STATEMENT_TIMEOUT_IN_SECONDS", "30")
    cursor.fetchall.return_value = [(1,)]

    def execute(sql):
        if sql == "alter session set statement_timeout_in_seconds = 30":
            raise RuntimeError("restore failed")

    cursor.execute.side_effect = execute
    connection = SimpleNamespace(
        cursor=lambda: cursor, close=Mock(), _network_timeout=65, _socket_timeout=65
    )
    if close_fails:
        connection.close.side_effect = RuntimeError("close failed")
    adapter = SnowflakeNativeAdapter("analytics")
    adapter._conn = connection
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1", limits={"statement_timeout_ms": 120000})
    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    connection.close.assert_called_once()
    assert adapter._conn is None
