"""Offline unit tests for the MotherDuck connector.

MotherDuck speaks DuckDB SQL, so the dialect contract is "identical to
DuckDB": MotherDuckDialect subclasses DuckDbDialect and overrides
NOTHING. Part (a) pins that, and the exact DuckDB SQL forms the
conformance suite depends on. Parts (b) and (c) cover the adapter
(option normalization, secret indirection, redacted errors, namespace
bootstrap) and a compile-only sweep of the full integration battery.

No network, no MotherDuck token: the driver is monkeypatched.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts import duckdb_setup
from semantic_rails.db_parts.motherduck import MotherDuckAdapter, create_adapter
from semantic_rails.dialects import (
    DuckDbDialect,
    MotherDuckDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.schema import ConnectionSpec, PackageMeta, SeedSpec
from semantic_rails.sql_ast import SqlIdentifier, SqlLiteral

MD = MotherDuckDialect()
COL = SqlIdentifier(["t", "c"])
OTHER = SqlIdentifier(["t", "d"])

# ---------------------------------------------------------------------------
# (a) Dialect rendering — MotherDuck is DuckDB SQL: it overrides no hook, so every
# rendering is DuckDB's own; the concrete forms are pinned once here for the family.
# ---------------------------------------------------------------------------


def test_registry_wires_motherduck_dialect_that_overrides_nothing():
    dialect = dialect_for_warehouse("motherduck")
    assert isinstance(dialect, MotherDuckDialect)
    assert isinstance(dialect, DuckDbDialect)  # inheritance IS the contract
    assert dialect.name == "motherduck"
    overridden = {
        name
        for name, value in vars(MotherDuckDialect).items()
        if callable(value) and not name.startswith("__")
    }
    assert overridden == set(), f"MotherDuckDialect must not override dialect hooks: {overridden}"

    connector = warehouse_connector("motherduck")
    assert connector is not None
    assert connector.connection_kinds == ("motherduck_native",)
    assert connector.adapter == "semantic_rails.db_parts.motherduck:create_adapter"
    assert set(connector.connection_options) == {"database", "schema", "token_env", "token_file"}


def test_exact_duckdb_sql_forms():
    # Pin the concrete DuckDB-semantics forms the conformance suite
    # relies on (linear-interpolation percentiles, ISO week truncation,
    # boundary-crossing date_diff, arg_min/arg_max row selection).
    assert render_expr(MD.percentile_cont(COL, 0.95)) == "QUANTILE_CONT(t.c, 0.95)"
    assert render_expr(MD.median(COL)) == "MEDIAN(t.c)"
    assert render_expr(MD.first_value(COL, OTHER)) == "arg_min(t.c, t.d)"
    assert render_expr(MD.last_value(COL, OTHER)) == "arg_max(t.c, t.d)"
    assert render_expr(MD.date_trunc("week", COL)) == "DATE_TRUNC('week', CAST(t.c AS TIMESTAMP))"
    assert (
        render_expr(MD.date_diff("day", COL, OTHER))
        == "DATE_DIFF('day', CAST(t.c AS TIMESTAMP), CAST(t.d AS TIMESTAMP))"
    )
    assert (
        render_expr(MD.date_add("month", SqlLiteral(3), COL)) == "DATE_ADD(t.c, INTERVAL (3) MONTH)"
    )
    assert render_expr(MD.null_safe_eq(COL, OTHER)) == "t.c IS NOT DISTINCT FROM t.d"
    assert (
        render_expr(MD.conditional_aggregate("count", MD.null_safe_eq(COL, OTHER), None))
        == "COUNT(CASE WHEN t.c IS NOT DISTINCT FROM t.d THEN 1 END)"
    )
    assert (
        render_expr(MD.conditional_aggregate("sum", MD.null_safe_eq(COL, OTHER), OTHER))
        == "SUM(CASE WHEN t.c IS NOT DISTINCT FROM t.d THEN t.d END)"
    )
    assert MD.timestamp_type_name() == "TIMESTAMP"
    assert render_expr(MD.timestamp_cast(COL)) == "CAST(t.c AS TIMESTAMP)"


# ---------------------------------------------------------------------------
# (b) Adapter — option normalization, secrets, redaction. Driver faked.
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.description = [("ONE",), ("TWO",)]
        self.fail_on = fail_on

    def execute(self, sql):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("driver exploded: relation does not exist")
        self.log.append(sql)

    def sql(self, sql, *, params):
        assert params == []
        self.execute(sql)
        return SimpleNamespace(description=self.description, fetchall=lambda: [(1, "x")])

    def close(self):
        self.log.append("<cursor closed>")


class FakeConnection:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on
        self.closed = False

    def execute(self, sql, parameters=None):
        self.log.append(sql)
        return self

    def fetchone(self):
        return ("", "")

    def fetchall(self):
        return []

    def cursor(self):
        return FakeCursor(self.log, fail_on=self.fail_on)

    def close(self):
        self.closed = True


def _fake_duckdb(monkeypatch, captured, *, fail_on=None):
    def connect(path, read_only=False, config=None):
        captured["path"] = path
        captured["config"] = dict(config or {})
        conn = FakeConnection(captured.setdefault("log", []), fail_on=fail_on)
        captured["conn"] = conn
        return conn

    monkeypatch.setattr(duckdb_setup, "duckdb", SimpleNamespace(connect=connect))


def _options(**overrides):
    base = {"token_env": "SR_MOTHERDUCK_TOKEN", "database": "sr_jaffle"}
    base.update(overrides)
    return base


def test_adapter_normalizes_options_and_reports_truthful_timeout_support():
    adapter = MotherDuckAdapter({"Token-Env": " SR_MOTHERDUCK_TOKEN ", "DATABASE": "sr_jaffle"})
    assert adapter.options == {"token_env": "SR_MOTHERDUCK_TOKEN", "database": "sr_jaffle"}
    assert adapter.supports_statement_timeout is False
    assert adapter.engine == "motherduck"
    assert adapter.connection_kind == "motherduck_native"


def test_adapter_rejects_incomplete_or_literal_secret_options():
    for options, named in [
        (_options(host="example.com"), "unsupported package.connection option 'host'"),
        ({"token": "md_live_supersecret", "database": "sr_jaffle"}, "token"),
        ({"database": "sr_jaffle"}, "token_env or token_file"),
        ({"token_env": "SR_MOTHERDUCK_TOKEN"}, "database"),
    ]:
        with pytest.raises(SemanticLayerError) as exc:
            MotherDuckAdapter(options)
        assert exc.value.code == "INVALID_CONFIG"
        # The message names what to fix (the details carry no option names); a literal
        # secret VALUE never appears in the error.
        assert named in str(exc.value)
        assert "md_live_supersecret" not in str(exc.value) + repr(exc.value.details)


def test_adapter_refuses_missing_token_and_unsafe_database_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict = {}
    _fake_duckdb(monkeypatch, captured)
    monkeypatch.delenv("SR_MOTHERDUCK_TOKEN", raising=False)

    with pytest.raises(SemanticLayerError) as exc:
        MotherDuckAdapter(_options()).query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == ["SR_MOTHERDUCK_TOKEN"]

    monkeypatch.setenv("SR_MOTHERDUCK_TOKEN", "tok-value")
    with pytest.raises(SemanticLayerError) as exc:
        MotherDuckAdapter(_options(database="bad name; DROP")).query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["option"] == "database"
    # Option VALUES never leak into the error.
    assert "DROP" not in str(exc.value)
    assert "path" not in captured  # never connected


def test_adapter_connects_with_token_and_defaults_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    captured: dict = {}
    _fake_duckdb(monkeypatch, captured)
    monkeypatch.setenv("SR_MOTHERDUCK_TOKEN", "tok-value")
    adapter = MotherDuckAdapter(_options(schema="analytics"))

    rows = adapter.query("select 1")

    assert rows == [{"ONE": 1, "TWO": "x"}]
    assert captured["path"] == "md:"
    assert captured["config"] == {"motherduck_token": "tok-value"}
    # Lazy database creation + namespace defaulting so unqualified
    # table names (jaffle_order, …) resolve.
    assert captured["log"][:9] == [
        "SELECT database_name, function_name, function_type FROM system.main.duckdb_functions()",
        "SELECT system.main.current_setting('disabled_optimizers')",
        "SET disabled_optimizers = ?",
        "SELECT system.main.current_setting('lock_configuration'), "
        "system.main.current_setting('allowed_configs')",
        "SET enable_progress_bar = false",
        'CREATE DATABASE IF NOT EXISTS "sr_jaffle"',
        'USE "sr_jaffle"',
        'CREATE SCHEMA IF NOT EXISTS "analytics"',
        'USE "sr_jaffle"."analytics"',
    ]
    assert "select 1" in captured["log"]

    adapter.close()
    assert captured["conn"].closed is True

    # A token file is read (trailing newline stripped) when no token env is set.
    monkeypatch.delenv("SR_MOTHERDUCK_TOKEN", raising=False)
    token_file = tmp_path / "md_token"
    token_file.write_text("file-token\n")
    MotherDuckAdapter({"token_file": str(token_file), "database": "sr_jaffle"}).query("select 1")
    assert captured["config"] == {"motherduck_token": "file-token"}


def test_adapter_redacts_query_errors(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _fake_duckdb(monkeypatch, captured, fail_on="jaffle_order")
    monkeypatch.setenv("SR_MOTHERDUCK_TOKEN", "tok-value")
    adapter = MotherDuckAdapter(_options())

    secret_sql = "SELECT secret_column FROM jaffle_order WHERE note = 'pii-literal'"
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query(secret_sql)

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "motherduck"
    assert details["connection_kind"] == "motherduck_native"
    assert details["option_keys"] == sorted(adapter.options)
    assert details["sql_redacted"] is True
    # Never raw SQL, option values, or rows in the details payload.
    assert "sql" not in details
    assert "pii-literal" not in repr(details)
    assert "tok-value" not in str(exc.value)
    assert "tok-value" not in repr(details)


def test_create_adapter_entry_point_via_registry_factory():
    package = PackageMeta(
        package_id="md_demo",
        name="md_demo",
        description="motherduck demo",
        warehouse="motherduck",
        connection=ConnectionSpec(kind="motherduck_native", options=_options()),
    )
    adapter = create_warehouse_adapter(package)
    assert isinstance(adapter, MotherDuckAdapter)
    assert adapter.options == _options()

    with pytest.raises(SemanticLayerError) as exc:
        create_adapter(replace(package, connection=ConnectionSpec(kind="duckdb_native")))
    assert exc.value.code == "INVALID_CONFIG"
    assert "duckdb_native" in str(exc.value)


# ---------------------------------------------------------------------------
# (c) Compile-only sweep — every battery case must compile for motherduck.
# ---------------------------------------------------------------------------


def _motherduck_config(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="motherduck",
            default_db="",
            seed=SeedSpec(),
            connection=ConnectionSpec(
                kind="motherduck_native",
                options={"token_env": "SR_MOTHERDUCK_TOKEN", "database": "sr_jaffle"},
            ),
        ),
    )


def test_motherduck_compiles_entire_integration_battery():
    from tests.integration.harness import PACKAGE_DIR, load_battery

    config = _motherduck_config(load_package_config(str(PACKAGE_DIR)))
    registry = Registry(config)
    battery = load_battery()
    assert battery, "battery must not be empty"

    duck_config = load_package_config(str(PACKAGE_DIR))
    duck_registry = Registry(duck_config)

    for case in battery:
        query, _ = read_call(case.payload, config=config)
        # One request instant for both compiles: a named window's coverage reads it.
        query["policy_context"] = {"now": "2026-10-01T00:00:00Z"}
        compiled = compile_query(config, registry, query)
        sql = compiled["sql"]
        assert isinstance(sql, str) and sql.strip(), f"{case.name}: empty rendered SQL"
        # MotherDuck IS DuckDB SQL — the rendered text must match the
        # DuckDB reference compilation exactly.
        duck_sql = compile_query(duck_config, duck_registry, query)["sql"]
        assert sql == duck_sql, f"{case.name}: motherduck SQL diverges from duckdb"
