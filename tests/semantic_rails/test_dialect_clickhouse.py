"""Offline unit tests for the ClickHouse connector.

Covers (a) dialect rendering for every overridden method, (b) adapter
option normalization / secret hygiene / error redaction with a faked
``clickhouse_connect`` driver, and (c) a compile-only sweep of the full
integration battery against the clickhouse dialect. The redirect
regressions use loopback HTTP only; no external network is required.
"""

from __future__ import annotations

import sys
import threading
import types
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.clickhouse import ClickHouseAdapter
from semantic_rails.dialects import (
    ClickHouseDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_ast import SqlIdentifier, SqlLiteral, SqlOrderTerm

DIALECT = ClickHouseDialect()


def _ident(name: str) -> SqlIdentifier:
    return SqlIdentifier(parts=[name])


# ---------------------------------------------------------------------------
# (a) Dialect rendering — one assertion per overridden method.
# ---------------------------------------------------------------------------


def _nullable(value: str) -> str:
    return f"CAST({value} AS Nullable(DateTime64(6)))"


def test_registry_returns_clickhouse_dialect_rendering_clickhouse_forms():
    assert isinstance(dialect_for_warehouse("clickhouse"), ClickHouseDialect)
    connector = warehouse_connector("clickhouse")
    assert connector is not None
    assert connector.connection_kinds == ("clickhouse_native",)
    assert connector.adapter == "semantic_rails.db_parts.clickhouse:create_adapter"

    rendered = [
        (DIALECT.percentile_cont(_ident("x"), 0.95), "quantileExactInclusive(0.95)(x)"),
        (DIALECT.median(_ident("x")), "quantileExactInclusive(0.5)(x)"),
        (DIALECT.first_value(_ident("x"), _ident("ts")), "argMin(x, ts)"),
        (DIALECT.last_value(_ident("x"), _ident("ts")), "argMax(x, ts)"),
        # ClickHouse date_trunc returns Date for week-and-coarser grains —
        # the outer cast restores timestamp parity with DuckDB.
        (
            DIALECT.date_trunc("month", _ident("ts")),
            "CAST(DATE_TRUNC('month', CAST(ts AS TIMESTAMP)) AS TIMESTAMP)",
        ),
        (DIALECT.null_safe_eq(_ident("a"), _ident("b")), "(a <=> b)"),
        # Nullable timestamp targets preserve missing endpoints independently of settings.
        (
            DIALECT.date_diff("day", _ident("s"), _ident("e")),
            f"DATE_DIFF('day', {_nullable('s')}, {_nullable('e')})",
        ),
        (DIALECT.date_add("day", SqlLiteral(3), _ident("d")), "DATE_ADD(d, INTERVAL (3) DAY)"),
        (
            DIALECT.conditional_aggregate("count", _ident("cond"), None),
            "COUNT(CASE WHEN cond THEN 1 END)",
        ),
        # ClickHouse 24.x has no LAG window function; window_lag builds the
        # exact-equivalent CASE/COUNT/argMin form (verified live against
        # lagInFrame, including partitions and NULL leading rows).
        (
            DIALECT.window_lag(
                _ident("m1"),
                12,
                partition_by=[_ident("g1")],
                order_by=[SqlOrderTerm(expr=_ident("t"), direction="ASC")],
            ),
            "CASE WHEN COUNT(1) OVER (PARTITION BY g1 ORDER BY t ASC "
            "ROWS BETWEEN 12 PRECEDING AND CURRENT ROW) = 13 "
            "THEN argMin(m1, t) OVER (PARTITION BY g1 ORDER BY t ASC "
            "ROWS BETWEEN 12 PRECEDING AND CURRENT ROW) END",
        ),
    ]
    # Authored DATE_DIFF keeps pre-epoch and NULL endpoints.
    for start, end in [
        ("1950-01-01", "2024-01-01"),
        ("2024-01-01", "1950-01-01"),
        (None, "1950-01-01"),
        ("1950-01-01", None),
        (None, None),
    ]:
        endpoints = [render_expr(SqlLiteral(start)), render_expr(SqlLiteral(end))]
        rendered.append(
            (
                DIALECT.scalar_call(
                    "DATE_DIFF", [SqlLiteral("year"), SqlLiteral(start), SqlLiteral(end)]
                ),
                f"DATE_DIFF('year', {_nullable(endpoints[0])}, {_nullable(endpoints[1])})",
            )
        )
    for expr, expected in rendered:
        assert render_expr(expr) == expected

    caps = DIALECT.capabilities()
    assert caps["timestamp_type"] == "TIMESTAMP"
    assert caps["percentile_cont"] is True
    assert caps["null_safe_equality_operator"] == "<=>"
    assert caps["qualify"] is False
    assert caps["snapshot_row_selection"] == "aggregate_join"


# ---------------------------------------------------------------------------
# (b) Adapter — options, secrets, redaction (driver faked in sys.modules).
# ---------------------------------------------------------------------------


class _FakeResult:
    column_names = ["one", "two"]
    result_rows = [(1, "x"), (2, "y")]


def _install_fake_driver(monkeypatch: pytest.MonkeyPatch, captured: dict, *, fail: str = ""):
    class FakePool:
        def request(self, method, url, **kwargs):
            captured.setdefault("requests", []).append((method, url, kwargs))
            return "response"

        def clear(self):
            captured["pool_cleared"] = True

    httputil = types.ModuleType("clickhouse_connect.driver.httputil")
    httputil.all_managers = {}
    httputil.check_env_proxy = lambda scheme, host, port: None

    def get_pool_manager(**kwargs):
        captured["pool_kwargs"] = kwargs
        pool = FakePool()
        httputil.all_managers[pool] = 1
        captured["pool"] = pool
        return pool

    httputil.get_pool_manager = get_pool_manager

    class FakeClient:
        from urllib3.util import Timeout

        timeout = Timeout(connect=10, read=65)

        def __init__(self, **kwargs):
            captured["connect_kwargs"] = kwargs
            self.http = kwargs["pool_mgr"]

        def query(self, sql, settings=None):
            captured["sql"] = sql
            captured["settings"] = settings
            if fail:
                raise RuntimeError(fail)
            return _FakeResult()

        def close(self):
            captured["closed"] = True

    module = types.ModuleType("clickhouse_connect")
    module.get_client = lambda **kwargs: FakeClient(**kwargs)
    driver = types.ModuleType("clickhouse_connect.driver")
    driver.httputil = httputil

    def parse_connection_params(**kwargs):
        port = kwargs["port"]
        interface = kwargs["interface"] or (
            "https" if kwargs["secure"] or port in (443, 8443) else "http"
        )
        return (
            kwargs["host"],
            kwargs["username"],
            kwargs["password"],
            port,
            kwargs["database"],
            interface,
        )

    driver._parse_connection_params = parse_connection_params
    module.driver = driver
    monkeypatch.setitem(sys.modules, "clickhouse_connect", module)
    monkeypatch.setitem(sys.modules, "clickhouse_connect.driver", driver)
    monkeypatch.setitem(sys.modules, "clickhouse_connect.driver.httputil", httputil)


def _adapter_options() -> dict[str, str]:
    return {
        "host_env": "SR_CH_TEST_HOST",
        "port": "9000",
        "database": "sr_jaffle",
        "user_env": "SR_CH_TEST_USER",
        "password_env": "SR_CH_TEST_PASSWORD",
    }


def _set_connection_env(monkeypatch: pytest.MonkeyPatch, password: str = "pw") -> None:
    monkeypatch.setenv("SR_CH_TEST_HOST", "ch.example.com")
    monkeypatch.setenv("SR_CH_TEST_USER", "svc_user")
    monkeypatch.setenv("SR_CH_TEST_PASSWORD", password)


def test_adapter_normalizes_connection_options_and_rejects_secrets():
    adapter = ClickHouseAdapter({"HOST-ENV": "SR_CH_TEST_HOST", "database": " sr_jaffle "})
    assert adapter.options == {"host_env": "SR_CH_TEST_HOST", "database": "sr_jaffle"}
    assert adapter.supports_statement_timeout is True

    for options, named in [
        ({"password": "hunter2"}, "password_env"),
        ({"warehouse_size": "XL"}, "unsupported package.connection option 'warehouse_size'"),
    ]:
        with pytest.raises(SemanticLayerError) as exc:
            ClickHouseAdapter(options)
        assert exc.value.code == "INVALID_CONFIG"
        assert named in str(exc.value)
        assert "hunter2" not in str(exc.value)

    package = PackageMeta(
        package_id="ch_demo",
        name="ch_demo",
        description="clickhouse demo",
        warehouse="clickhouse",
        connection=ConnectionSpec(kind="clickhouse_native", options=_adapter_options()),
    )
    adapter = create_warehouse_adapter(package)
    assert isinstance(adapter, ClickHouseAdapter)
    assert adapter.options == _adapter_options()


def test_adapter_reports_missing_env_without_leaking_values(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    monkeypatch.setenv("SR_CH_TEST_HOST", "ch.example.com")
    monkeypatch.setenv("SR_CH_TEST_PASSWORD", "super-secret")
    monkeypatch.delenv("SR_CH_TEST_USER", raising=False)

    adapter = ClickHouseAdapter(_adapter_options())
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == ["SR_CH_TEST_USER"]
    assert exc.value.details["connection_kind"] == "clickhouse_native"
    assert "super-secret" not in str(exc.value)
    assert "super-secret" not in repr(exc.value.details)


def test_adapter_queries_and_maps_rows(monkeypatch: pytest.MonkeyPatch):
    _set_connection_env(monkeypatch)
    for timeouts, connect_timeout, read_timeout in [
        ({}, 10, 65),
        ({"connect_timeout_seconds": "7", "read_timeout_seconds": "45"}, 7, 45),
    ]:
        captured: dict = {}
        _install_fake_driver(monkeypatch, captured)
        adapter = ClickHouseAdapter({**_adapter_options(), **timeouts})
        rows = adapter.query("select 1")
        adapter.close()

        assert rows == [{"one": 1, "two": "x"}, {"one": 2, "two": "y"}]
        kwargs = captured["connect_kwargs"]
        assert kwargs["host"] == "ch.example.com"
        assert kwargs["port"] == 9000
        assert kwargs["username"] == "svc_user"
        assert kwargs["password"] == "pw"
        assert kwargs["database"] == "sr_jaffle"
        assert kwargs["secure"] is False
        assert kwargs["settings"] == {"allow_experimental_join_condition": 1}
        assert kwargs["connect_timeout"] == connect_timeout
        assert kwargs["send_receive_timeout"] == read_timeout
        assert captured["settings"] is None  # no limits -> no per-query settings
        assert captured["closed"] is True

    # A request's limits become the statement's settings and clip the rows.
    rows = ClickHouseAdapter(_adapter_options()).query(
        "select 1", limits={"statement_timeout_ms": 2500, "max_rows": 1}
    )
    assert captured["settings"] == {"max_execution_time": 3}
    assert rows == [{"one": 1, "two": "x"}]


def test_adapter_redacts_query_errors(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured, fail="boom " + "x" * 600)
    _set_connection_env(monkeypatch, password="super-secret")

    adapter = ClickHouseAdapter(_adapter_options())
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select * from secret_table")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "clickhouse"
    assert details["connection_kind"] == "clickhouse_native"
    assert details["option_keys"] == sorted(_adapter_options())
    assert details["sql_redacted"] is True
    assert "sql" not in details
    assert "boom" not in str(exc.value)
    assert "super-secret" not in str(exc.value)
    assert "secret_table" not in repr(details)


# ---------------------------------------------------------------------------
# (c) Compile-only sweep — the full integration battery compiles to
# non-empty SQL for the clickhouse dialect. No warehouse connection.
# ---------------------------------------------------------------------------


def _clickhouse_config(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="clickhouse",
            default_db="",
            seed=replace(config.package.seed, kind="", source="", post_sql=""),
            connection=ConnectionSpec(
                kind="clickhouse_native",
                options={
                    "host_env": "SR_CLICKHOUSE_HOST",
                    "database": "sr_jaffle",
                    "user_env": "SR_CLICKHOUSE_USER",
                    "password_env": "SR_CLICKHOUSE_PASSWORD",
                },
            ),
        ),
    )


def test_battery_compiles_for_clickhouse():
    from tests.integration.harness import load_battery

    config = _clickhouse_config(
        load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    )
    registry = Registry(config)
    cases = load_battery()
    assert cases, "battery must not be empty"
    compiled: dict[str, str] = {}
    refused: list[str] = []
    for case in cases:
        try:
            result = compile_query(config, registry, read_call(case.payload, config=config)[0])
        except SemanticLayerError as exc:
            # ClickHouse has no implicit calendar, which every filled series uses.
            assert exc.code == "REWRITE_NOT_SUPPORTED", (case.name, exc)
            assert exc.details == {"warehouse": "clickhouse", "rewrite": "day_series"}
            refused.append(case.name)
            continue
        sql = result["sql"]
        assert isinstance(sql, str) and sql.strip(), f"{case.name}: empty SQL"
        compiled[case.name] = sql

    assert compiled and len(refused) < len(cases)
    all_sql = "\n".join(compiled.values())
    # ClickHouse SQL must use the dialect's forms, not DuckDB's.
    assert "QUANTILE_CONT(" not in all_sql
    assert "arg_min(" not in all_sql
    assert "arg_max(" not in all_sql
    assert "IS NOT DISTINCT FROM" not in all_sql
    assert "MEDIAN(" not in all_sql
    # ClickHouse 24.x has no LAG window function — prior_period must
    # compile through the window_lag emulation instead.
    assert "LAG(" not in all_sql


# ---------------------------------------------------------------------------
# (c) Connections stay on the configured host: redirects are not followed.
# ---------------------------------------------------------------------------


def test_adapter_client_pool_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    module = sys.modules["clickhouse_connect"]
    make_client = module.get_client

    def get_client(**kwargs):
        # Real clickhouse-connect sends its first request inside get_client.
        kwargs["pool_mgr"].request("POST", "http://ch:8123/", body=b"init", retries=2)
        return make_client(**kwargs)

    module.get_client = get_client
    _set_connection_env(monkeypatch)

    adapter = ClickHouseAdapter(_adapter_options())
    client = adapter._client_handle()

    assert captured["requests"] == [
        ("POST", "http://ch:8123/", {"body": b"init", "retries": 2, "redirect": False})
    ]
    assert client.http is captured["pool"]
    adapter.close()
    assert captured["pool_cleared"] is True
    assert captured["pool"] not in sys.modules["clickhouse_connect.driver.httputil"].all_managers


def test_adapter_uses_driver_https_proxy_pool(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    httputil = sys.modules["clickhouse_connect.driver.httputil"]

    def proxy_for(scheme, host, port):
        captured["proxy_lookup"] = (scheme, host, port)
        return "http://proxy.local:8080"

    httputil.check_env_proxy = proxy_for
    _set_connection_env(monkeypatch)
    adapter = ClickHouseAdapter({**_adapter_options(), "secure": "true"})
    adapter._client_handle()
    adapter.close()

    assert captured["proxy_lookup"] == ("https", "ch.example.com", 9000)
    assert captured["pool_kwargs"] == {"https_proxy": "http://proxy.local:8080"}
    assert captured["connect_kwargs"]["pool_mgr"] is captured["pool"]


@pytest.mark.parametrize(
    ("port", "secure_option", "expected_scheme"),
    [
        (443, None, "https"),
        (8443, "false", "https"),
        (9000, "true", "https"),
        (9000, "false", "http"),
    ],
)
def test_adapter_proxy_matches_driver_interface(
    monkeypatch: pytest.MonkeyPatch, port: int, secure_option: str | None, expected_scheme: str
):
    driver = pytest.importorskip("clickhouse_connect")
    from clickhouse_connect.driver import httputil

    proxy_urls = {
        "http": "http://127.0.0.1:11111",
        "https": "http://127.0.0.1:22222",
    }
    captured: dict = {}

    class FakePool:
        def request(self, *args, **kwargs):
            pytest.fail("pool construction must not make a network request")

        def clear(self):
            captured["cleared"] = True

    def get_pool_manager(**kwargs):
        captured["pool_kwargs"] = kwargs
        pool = FakePool()
        httputil.all_managers[pool] = 1
        return pool

    def get_client(**kwargs):
        captured["connect_kwargs"] = kwargs
        return types.SimpleNamespace(http=kwargs["pool_mgr"], close=lambda: None)

    monkeypatch.setattr(httputil, "check_env_proxy", lambda scheme, host, port: proxy_urls[scheme])
    monkeypatch.setattr(httputil, "get_pool_manager", get_pool_manager)
    monkeypatch.setattr(driver, "get_client", get_client)
    monkeypatch.setenv("SR_CH_TEST_HOST", "127.0.0.1")
    monkeypatch.setenv("SR_CH_TEST_USER", "svc_user")
    monkeypatch.setenv("SR_CH_TEST_PASSWORD", "pw")
    options = {**_adapter_options(), "port": str(port)}
    if secure_option is not None:
        options["secure"] = secure_option

    adapter = ClickHouseAdapter(options)
    adapter._client_handle()
    adapter.close()

    assert captured["pool_kwargs"] == {f"{expected_scheme}_proxy": proxy_urls[expected_scheme]}
    assert captured["connect_kwargs"]["pool_mgr"] is not None
    assert captured["cleared"] is True


def test_no_redirect_pool_returns_the_redirect_instead_of_following_it():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import urllib3

    from semantic_rails.db_parts.clickhouse import _no_redirect_pool

    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            hits.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/start"
    try:
        followed = urllib3.PoolManager().request("GET", url)
        hits_when_followed = list(hits)
        hits.clear()
        manager = urllib3.PoolManager()
        httputil = types.SimpleNamespace(
            check_env_proxy=lambda scheme, host, port: None,
            get_pool_manager=lambda **kwargs: manager,
        )

        def parse_connection_params(**kwargs):
            return (kwargs["host"], None, "", kwargs["port"], "", "http")

        kept = _no_redirect_pool(
            httputil,
            parse_connection_params,
            {"host": "127.0.0.1", "port": server.server_port, "secure": False},
        ).request("GET", url)
    finally:
        server.shutdown()
        server.server_close()

    assert (followed.status, hits_when_followed) == (200, ["/start", "/elsewhere"])
    assert (kept.status, hits) == (302, ["/start"])


def test_real_driver_constructor_cannot_follow_redirect(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("clickhouse_connect")
    from clickhouse_connect.driver import httputil

    managers_before = set(httputil.all_managers)
    hits: list[str] = []

    class Destination(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            hits.append("destination")
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    destination = HTTPServer(("127.0.0.1", 0), Destination)

    class Configured(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            hits.append("configured")
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/target")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    configured = HTTPServer(("127.0.0.1", 0), Configured)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (destination, configured)
    ]
    for thread in threads:
        thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("SR_CH_TEST_HOST", "127.0.0.1")
    monkeypatch.setenv("SR_CH_TEST_USER", "svc_user")
    monkeypatch.setenv("SR_CH_TEST_PASSWORD", "pw")
    try:
        options = {**_adapter_options(), "port": str(configured.server_port)}
        with pytest.raises(SemanticLayerError) as exc:
            ClickHouseAdapter(options).query("select 1")
    finally:
        for server in (configured, destination):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    assert hits == ["configured"]
    assert set(httputil.all_managers) == managers_before


@pytest.mark.parametrize("timeout_ms, expected_wait", [(1000, 65), (120000, 125)])
def test_clickhouse_request_deadline_extends_and_restores_client_wait(timeout_ms, expected_wait):
    adapter = ClickHouseAdapter()
    from urllib3.util import Timeout

    original = Timeout(connect=10, read=65)
    client = types.SimpleNamespace(timeout=original)

    def query(sql, *, settings):
        assert client.timeout.read_timeout == expected_wait
        assert settings == {"max_execution_time": timeout_ms // 1000}
        return types.SimpleNamespace(column_names=["value"], result_rows=[[1]])

    client.query = query
    adapter._client = client
    assert adapter.query("select 1", limits={"statement_timeout_ms": timeout_ms}) == [{"value": 1}]
    assert client.timeout is original
