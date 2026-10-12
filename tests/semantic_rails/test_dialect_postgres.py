"""Offline unit tests for the Postgres dialect + adapter.

No network: the driver is faked through ``sys.modules`` and the compile
sweep only renders SQL. Live execution is covered by
``tests/integration`` (target ``postgres``).
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.adbc import AdbcAdapter
from semantic_rails.db_parts.postgres import create_adapter
from semantic_rails.dialects import (
    PostgresDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_ast import SqlBinary, SqlIdentifier, SqlLiteral

DIALECT = PostgresDialect()
X = SqlIdentifier(parts=["t", "x"])
ORDER = SqlIdentifier(parts=["t", "snapshot_at"])
START = SqlIdentifier(parts=["t", "started_at"])
END = SqlIdentifier(parts=["t", "ended_at"])

START_TS = "CAST(t.started_at AS TIMESTAMP)"
END_TS = "CAST(t.ended_at AS TIMESTAMP)"


# ---------------------------------------------------------------------------
# Dialect rendering — every overridden method, every date unit.
# ---------------------------------------------------------------------------


def test_registry_wires_postgres_dialect_and_adapter():
    connector = warehouse_connector("postgres")
    assert connector is not None
    assert isinstance(connector.dialect, PostgresDialect)
    assert connector.connection_kinds == ("postgres_native",)
    assert connector.adapter == "semantic_rails.db_parts.postgres:create_adapter"
    assert isinstance(dialect_for_warehouse("postgres"), PostgresDialect)
    capabilities = DIALECT.capabilities()
    assert capabilities["percentile_cont"] is True
    assert capabilities["null_safe_equality_function"] == "IS NOT DISTINCT FROM"
    assert capabilities["timestamp_type"] == "TIMESTAMP"
    assert capabilities["qualify"] is False


def _epoch_delta(unit: str) -> str:
    return (
        f"DATE_PART('epoch', DATE_TRUNC('{unit}', {END_TS}))"
        f" - DATE_PART('epoch', DATE_TRUNC('{unit}', {START_TS}))"
    )


def test_dialect_renders_postgres_forms():
    condition = SqlBinary(X, ">", SqlLiteral(0))
    rendered = [
        (DIALECT.percentile_cont(X, 0.95), "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY t.x ASC)"),
        (DIALECT.median(X), "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.x ASC)"),
        (DIALECT.first_value(X, ORDER), "(ARRAY_AGG(t.x ORDER BY t.snapshot_at ASC))[1]"),
        (DIALECT.last_value(X, ORDER), "(ARRAY_AGG(t.x ORDER BY t.snapshot_at DESC))[1]"),
        (
            DIALECT.date_diff("day", START, END),
            f"CAST({END_TS} AS DATE) - CAST({START_TS} AS DATE)",
        ),
        (
            DIALECT.date_diff("week", START, END),
            f"(CAST({END_TS} AS DATE) - CAST({START_TS} AS DATE)) / 7",
        ),
        (
            DIALECT.date_diff("month", START, END),
            f"CAST((DATE_PART('year', {END_TS}) - DATE_PART('year', {START_TS})) * 12 + (DATE_PART('month', {END_TS}) - DATE_PART('month', {START_TS})) AS BIGINT)",
        ),
        (
            DIALECT.date_diff("quarter", START, END),
            f"CAST((DATE_PART('year', {END_TS}) - DATE_PART('year', {START_TS})) * 4 + (DATE_PART('quarter', {END_TS}) - DATE_PART('quarter', {START_TS})) AS BIGINT)",
        ),
        (
            DIALECT.date_diff("year", START, END),
            f"CAST(DATE_PART('year', {END_TS}) - DATE_PART('year', {START_TS}) AS BIGINT)",
        ),
        (DIALECT.date_diff("second", START, END), f"CAST({_epoch_delta('second')} AS BIGINT)"),
        (
            DIALECT.date_diff("minute", START, END),
            f"CAST(({_epoch_delta('minute')}) / 60 AS BIGINT)",
        ),
        (DIALECT.date_diff("hour", START, END), f"CAST(({_epoch_delta('hour')}) / 3600 AS BIGINT)"),
        # FULL JOIN needs both a hash-joinable clause and exact NULL equality.
        (
            DIALECT.null_safe_eq(X, ORDER),
            "COALESCE(CAST(t.x AS TEXT), '__sr_null__') = COALESCE(CAST(t.snapshot_at AS TEXT), '__sr_null__') AND t.x IS NOT DISTINCT FROM t.snapshot_at",
        ),
        (
            DIALECT.conditional_aggregate("sum", condition, ORDER),
            "SUM(CASE WHEN t.x > 0 THEN t.snapshot_at END)",
        ),
        (
            DIALECT.conditional_aggregate("count", condition, None),
            "COUNT(CASE WHEN t.x > 0 THEN 1 END)",
        ),
        (DIALECT.timestamp_cast(X), "CAST(t.x AS TIMESTAMP)"),
    ]
    # Fixed-length units add a scaled timestamp difference: `INTERVAL (n) UNIT` is a
    # Postgres syntax error (the parenthesized form is a precision spec).
    for unit, upper, lower in [
        ("day", "2000-01-02", "2000-01-01"),
        ("week", "2000-01-08", "2000-01-01"),
        ("hour", "2000-01-01 01:00:00", "2000-01-01 00:00:00"),
        ("minute", "2000-01-01 00:01:00", "2000-01-01 00:00:00"),
        ("second", "2000-01-01 00:00:01", "2000-01-01 00:00:00"),
    ]:
        rendered.append(
            (
                DIALECT.date_add(unit, SqlLiteral(3), X),
                f"CAST(t.x AS TIMESTAMP) + 3 * (CAST('{upper}' AS TIMESTAMP) - CAST('{lower}' AS TIMESTAMP))",
            )
        )
    for expr, expected in rendered:
        assert render_expr(expr) == expected
    assert DIALECT.timestamp_type_name() == "TIMESTAMP"

    for unit in ("month", "quarter", "year"):
        sql = render_expr(DIALECT.date_add(unit, SqlLiteral(2), X))
        # Target month rebuilt as text (PG renders no INTERVAL literal
        # through this AST) with day-of-month clamping for short months.
        assert "CONCAT(" in sql
        assert "'-01') AS TIMESTAMP)" in sql
        assert "CASE WHEN DATE_PART('day', CAST(t.x AS TIMESTAMP)) <=" in sql
        assert "FLOOR(" in sql
        assert "INTERVAL" not in sql and "DATE_ADD(" not in sql and "DATEADD(" not in sql, unit


# ---------------------------------------------------------------------------
# Adapter — option normalization, secrets contract, redaction.
# ---------------------------------------------------------------------------

VALID_OPTIONS = {
    "host_env": "SR_PG_TEST_HOST",
    "port": "5433",
    "database": "sr_jaffle",
    "schema": "analytics",
    "user_env": "SR_PG_TEST_USER",
    "password_env": "SR_PG_TEST_PASSWORD",
}


def _set_pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SR_PG_TEST_HOST", "pg.example.test")
    monkeypatch.setenv("SR_PG_TEST_USER", "svc_user")
    monkeypatch.setenv("SR_PG_TEST_PASSWORD", "super-secret")


def _install_fake_adbc(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    from types import SimpleNamespace

    from tests.semantic_rails.test_adbc_adapter import Batch, Reader

    class FakeCursor:
        adbc_statement = SimpleNamespace(set_options=lambda **kwargs: None)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            captured["cursor_closed"] = True

        def execute(self, sql, parameters=None):
            captured.setdefault("sql", []).append((sql, parameters))
            if captured.get("raise_on") and captured["raise_on"] in sql:
                raise RuntimeError("connection reset by peer")

        def fetchone(self):
            return ("UTC", "5s")

        def fetch_record_batch(self):
            return Reader([Batch([{"one": 1, "two": "x"}])])

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["connection_closed"] = True

    module = types.ModuleType("adbc_driver_postgresql.dbapi")

    def connect(uri, **kwargs):
        captured.update(uri=uri, kwargs=kwargs)
        return FakeConnection()

    module.connect = connect
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql.dbapi", module)


def test_adapter_normalizes_options_and_escapes_libpq_values(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    adapter = AdbcAdapter(
        {
            "HOST-ENV": "SR_PG_TEST_HOST",
            **{k: v for k, v in VALID_OPTIONS.items() if k != "host_env"},
        }
    )
    assert adapter.options == VALID_OPTIONS
    assert adapter._connect_uri() == (
        "host='pg.example.test' user='svc_user' password='super-secret' "
        "port='5433' connect_timeout='10' keepalives='1' keepalives_idle='65' "
        "keepalives_interval='1' keepalives_count='1' dbname='sr_jaffle'"
    )
    monkeypatch.setenv("SR_PG_TEST_PASSWORD", "quote' and slash\\")
    assert "password='quote\\' and slash\\\\'" in adapter._connect_uri()

    options = {key: value for key, value in VALID_OPTIONS.items() if key != "port"}
    options.update({"sslmode": "require", "statement_timeout_seconds": "30"})
    uri = AdbcAdapter(options)._connect_uri()
    assert "port='5432'" in uri
    assert "sslmode='require'" in uri


def test_adapter_rejects_unsupported_and_literal_secret_options():
    for option, value, named in [
        ("warehouse", "COMPUTE_WH", "unsupported package.connection option 'warehouse'"),
        ("password", "hunter2", "password"),
    ]:
        with pytest.raises(SemanticLayerError) as exc:
            AdbcAdapter({**VALID_OPTIONS, option: value})
        assert exc.value.code == "INVALID_CONFIG"
        assert named in str(exc.value)
        assert "hunter2" not in str(exc.value) + repr(exc.value.details)


def test_adapter_rejects_invalid_numeric_options_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_pg_env(monkeypatch)
    for option, value in [
        ("port", "fivefourthreetwo"),
        ("connect_timeout_seconds", "0"),
        ("read_timeout_seconds", "-1"),
        ("connect_timeout_seconds", "invalid"),
    ]:
        captured: dict = {}
        _install_fake_adbc(monkeypatch, captured)
        with pytest.raises(SemanticLayerError) as exc:
            AdbcAdapter({**VALID_OPTIONS, option: value}).query("select 1")
        assert exc.value.code == "INVALID_CONFIG"
        assert exc.value.details["option"] == option
        # Option VALUES never appear in error envelopes.
        assert value not in str(exc.value)
        assert "uri" not in captured


def test_adapter_reports_every_missing_env_var_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
):
    for name in ("SR_PG_TEST_HOST", "SR_PG_TEST_USER", "SR_PG_TEST_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    adapter = AdbcAdapter(VALID_OPTIONS)

    with pytest.raises(SemanticLayerError) as exc:
        adapter._connect_uri()

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == [
        "SR_PG_TEST_HOST",
        "SR_PG_TEST_PASSWORD",
        "SR_PG_TEST_USER",
    ]
    assert exc.value.details["engine"] == "postgres"
    assert exc.value.details["connection_kind"] == "postgres_native"


def test_adapter_queries_with_fake_driver_and_sets_then_restores_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_pg_env(monkeypatch)
    set_timeout = "SELECT set_config('statement_timeout', $1, false)"
    for timeouts, limits, connect_timeout, read_timeout, statement_timeout in [
        ({}, None, 10, 65, None),
        ({"statement_timeout_seconds": "0"}, None, 10, 65, None),
        ({"statement_timeout_seconds": "30"}, None, 10, 65, "30000"),
        ({"statement_timeout_seconds": "120"}, None, 10, 125, "120000"),
        (
            {
                "connect_timeout_seconds": "7",
                "read_timeout_seconds": "45",
                "statement_timeout_seconds": "40",
            },
            None,
            7,
            45,
            "40000",
        ),
        # A request's deadline wins over the configured one; none (or 0) keeps it.
        ({}, {"statement_timeout_ms": 1500}, 10, 65, "1500"),
        ({"statement_timeout_seconds": "30"}, {"statement_timeout_ms": 0}, 10, 65, "30000"),
        ({"statement_timeout_seconds": "30"}, {"statement_timeout_ms": 250}, 10, 65, "250"),
    ]:
        captured: dict = {}
        _install_fake_adbc(monkeypatch, captured)
        adapter = AdbcAdapter({**VALID_OPTIONS, **timeouts})
        assert adapter.supports_statement_timeout is True

        rows = adapter.query("select 1", limits=limits)
        adapter.close()

        assert rows == [{"one": 1, "two": "x"}]
        assert captured["kwargs"]["autocommit"] is True
        assert captured["sql"][0] == (
            "SELECT set_config('search_path', $1, false)",
            ('"analytics"',),
        )
        assert f"connect_timeout='{connect_timeout}'" in captured["uri"]
        assert f"keepalives_idle='{read_timeout}'" in captured["uri"]
        expected = [
            ("SELECT current_setting('TimeZone'), current_setting('statement_timeout')", None),
            ("select 1", None),
        ]
        if statement_timeout is not None:
            expected.insert(1, (set_timeout, (statement_timeout,)))
            expected.append((set_timeout, ("5s",)))
        assert captured["sql"][1:] == expected, (timeouts, limits)
        assert captured["cursor_closed"] is True
        assert captured["connection_closed"] is True


def test_adapter_redacts_query_errors(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    captured: dict = {"raise_on": "jaffle"}
    _install_fake_adbc(monkeypatch, captured)
    adapter = AdbcAdapter(VALID_OPTIONS)

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("SELECT secret_col FROM jaffle_orders_internal")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "postgres"
    assert details["connection_kind"] == "postgres_native"
    assert details["option_keys"] == sorted(VALID_OPTIONS)
    assert details["sql_redacted"] is True
    assert "sql" not in details
    # Neither raw SQL nor option values may leak into the envelope.
    rendered = str(exc.value) + repr(details)
    assert "jaffle_orders_internal" not in rendered
    assert "secret_col" not in rendered
    assert "pg.example.test" not in rendered
    assert "super-secret" not in rendered


def test_missing_driver_maps_to_missing_dependency(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql.dbapi", None)
    adapter = AdbcAdapter(VALID_OPTIONS)

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "MISSING_DEPENDENCY"
    assert "semantic-rails[postgres]" in str(exc.value)


def _package(kind: str) -> PackageMeta:
    return PackageMeta(
        package_id="pg_demo",
        name="pg_demo",
        description="postgres demo",
        warehouse="postgres",
        connection=ConnectionSpec(kind=kind, options=dict(VALID_OPTIONS)),
    )


def test_create_warehouse_adapter_selects_postgres_native():
    adapter = create_warehouse_adapter(_package("postgres_native"))

    assert isinstance(adapter, AdbcAdapter)
    assert adapter.options == VALID_OPTIONS

    for kind in ["postgres_cli", "removed_kind"]:
        with pytest.raises(SemanticLayerError) as exc:
            create_adapter(_package(kind))
        assert exc.value.code == "INVALID_CONFIG"
        assert exc.value.details["connection_kind"] == kind


# ---------------------------------------------------------------------------
# Adapter SQL compatibility pass (identifier length, float division).
# ---------------------------------------------------------------------------


def test_compat_pass_shortens_colliding_long_identifiers():
    from semantic_rails.dialects import dialect_for_warehouse

    name_a = "conversion_leaf_1__" + "a" * 50 + "_first"
    name_b = "conversion_leaf_1__" + "a" * 50 + "_second"
    assert len(name_a) > 63 and name_a[:63] == name_b[:63]  # would collide raw
    sql = (
        f"WITH {name_a} AS (SELECT 1 AS v), {name_b} AS (SELECT 2 AS v)\n"
        f"SELECT {name_a}.v FROM {name_a} CROSS JOIN {name_b}"
    )
    rewritten = dialect_for_warehouse("postgres").prepare_query(sql).sql

    new_names = {
        token
        for token in set(rewritten.replace("(", " ").replace(")", " ").split())
        if token.startswith("conversion_leaf_1__")
    }
    short_a = sorted(name for name in new_names if name.endswith(tuple("0123456789abcdef")))
    assert name_a not in rewritten and name_b not in rewritten
    # Both names shortened to <= 63 bytes, stay distinct, rewrite
    # consistently (definition + every reference).
    assert len(short_a) >= 2
    for name in new_names:
        bare = name.split(".")[0]
        assert len(bare.encode()) <= 63
        assert rewritten.count(bare) >= 1


def test_compat_pass_keeps_short_identifiers_and_literals_and_floats_nullif_divisions():
    from semantic_rails.dialects import dialect_for_warehouse

    prepare = dialect_for_warehouse("postgres").prepare_query
    long_in_literal = "x" * 80
    sql = f"SELECT name FROM jaffle_order WHERE name = '{long_in_literal}'"
    assert prepare(sql).sql == sql
    assert prepare("SELECT a / NULLIF(b / NULLIF(c, 0), 0), ') / NULLIF(' FROM t").sql == (
        "SELECT a / CAST(NULLIF(b / CAST(NULLIF(c, 0) AS DOUBLE PRECISION), 0)"
        " AS DOUBLE PRECISION), ') / NULLIF(' FROM t"
    )


# ---------------------------------------------------------------------------
# Compile-only sweep — the full conformance battery renders for postgres.
# ---------------------------------------------------------------------------


def _postgres_config(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="postgres",
            default_db="",
            seed=replace(config.package.seed, kind="", source="", post_sql=""),
            connection=ConnectionSpec(
                kind="postgres_native",
                options={
                    "host_env": "SR_POSTGRES_HOST",
                    "port": "5433",
                    "database": "sr_jaffle",
                    "schema": "public",
                    "user_env": "SR_POSTGRES_USER",
                    "password_env": "SR_POSTGRES_PASSWORD",
                },
            ),
        ),
    )


def test_postgres_compiles_every_battery_case():
    from tests.integration.harness import load_battery

    config = _postgres_config(
        load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    )
    registry = Registry(config)
    cases = load_battery()
    assert cases, "battery must not be empty"

    for case in cases:
        query, _ = read_call(case.payload, config=config)
        sql = compile_query(config, registry, query)["sql"]
        assert sql.strip(), f"{case.name}: empty SQL"
        for forbidden in (
            "DATE_DIFF(",
            "DATEDIFF(",
            "QUANTILE_CONT(",
            "arg_min(",
            "arg_max(",
            "MIN_BY(",
            "MAX_BY(",
            "MEDIAN(",
            "INTERVAL",  # `INTERVAL (n) UNIT` is unparseable on PG
            "TIMESTAMP_NTZ",
            "EQUAL_NULL(",
        ):
            assert forbidden not in sql, f"{case.name}: forbidden fragment {forbidden}\n{sql}"
