"""Offline unit tests for the MotherDuck connector.

MotherDuck speaks DuckDB SQL, so the dialect contract is "identical to
DuckDB": MotherDuckDialect subclasses DuckDbDialect and overrides
NOTHING. Part (a) pins that — every dialect hook renders byte-identical
to the DuckDB reference dialect, including the exact DuckDB SQL forms
the conformance suite depends on. Parts (b) and (c) cover the adapter
(option normalization, secret indirection, redacted errors, namespace
bootstrap) and a compile-only sweep of the full integration battery.

No network, no MotherDuck token: the driver is monkeypatched.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import semantic_rails.db_parts.motherduck as motherduck_module
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.motherduck import MotherDuckAdapter, create_adapter
from semantic_rails.dialects import (
    DuckDbDialect,
    MotherDuckDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.schema import ConnectionSpec, PackageMeta, SeedSpec
from semantic_rails.sql_ast import SqlIdentifier, SqlLiteral

MD = MotherDuckDialect()
DUCK = DuckDbDialect()
COL = SqlIdentifier(["t", "c"])
OTHER = SqlIdentifier(["t", "d"])

# Units the compiler actually emits through date_diff/date_add (grep
# compiler.py / compiler_parts / relation_pipelines for the call sites)
# plus every grain the time spec accepts.
DATE_UNITS = ("second", "minute", "hour", "day", "week", "month", "quarter", "year")


# ---------------------------------------------------------------------------
# (a) Dialect rendering — MotherDuck must be byte-identical to DuckDB.
# ---------------------------------------------------------------------------


def test_registry_wires_motherduck_dialect_and_adapter():
    dialect = dialect_for_warehouse("motherduck")
    assert isinstance(dialect, MotherDuckDialect)
    assert isinstance(dialect, DuckDbDialect)  # inheritance IS the contract
    assert dialect.name == "motherduck"

    connector = warehouse_connector("motherduck")
    assert connector is not None
    assert connector.connection_kinds == ("motherduck_native",)
    assert connector.adapter == "semantic_rails.db_parts.motherduck:create_adapter"
    assert set(connector.connection_options) == {"database", "schema", "token_env", "token_file"}


def test_motherduck_dialect_overrides_nothing():
    # The whole dialect contract: NO method overrides beyond the name.
    overridden = {
        name
        for name, value in vars(MotherDuckDialect).items()
        if callable(value) and not name.startswith("__")
    }
    assert overridden == set(), f"MotherDuckDialect must not override dialect hooks: {overridden}"


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("percentile", lambda d: d.percentile_cont(COL, 0.95)),
        ("median", lambda d: d.median(COL)),
        ("first_value", lambda d: d.first_value(COL, OTHER)),
        ("last_value", lambda d: d.last_value(COL, OTHER)),
        ("null_safe_eq", lambda d: d.null_safe_eq(COL, OTHER)),
        ("timestamp_cast", lambda d: d.timestamp_cast(COL)),
        ("convert_timezone", lambda d: d.convert_timezone("UTC", "America/New_York", COL)),
        (
            "cond_count",
            lambda d: d.conditional_aggregate("count", d.null_safe_eq(COL, OTHER), None),
        ),
        ("cond_sum", lambda d: d.conditional_aggregate("sum", d.null_safe_eq(COL, OTHER), OTHER)),
    ],
)
def test_every_dialect_hook_matches_duckdb(label, build):
    assert render_expr(build(MD)) == render_expr(build(DUCK)), label


@pytest.mark.parametrize("unit", DATE_UNITS)
def test_date_functions_match_duckdb_for_every_unit(unit):
    for build in (
        lambda d: d.date_trunc(unit, COL),
        lambda d: d.date_diff(unit, COL, OTHER),
        lambda d: d.date_add(unit, SqlLiteral(3), COL),
    ):
        assert render_expr(build(MD)) == render_expr(build(DUCK))


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
    assert MD.timestamp_type_name() == "TIMESTAMP"


def test_capabilities_match_duckdb():
    assert MD.capabilities() == DUCK.capabilities()


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

    def fetchall(self):
        return [(1, "x")]

    def close(self):
        self.log.append("<cursor closed>")


class FakeConnection:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on
        self.closed = False

    def execute(self, sql):
        self.log.append(sql)

    def cursor(self):
        return FakeCursor(self.log, fail_on=self.fail_on)

    def close(self):
        self.closed = True


def _fake_duckdb(monkeypatch, captured, *, fail_on=None):
    def connect(path, config=None):
        captured["path"] = path
        captured["config"] = dict(config or {})
        conn = FakeConnection(captured.setdefault("log", []), fail_on=fail_on)
        captured["conn"] = conn
        return conn

    monkeypatch.setattr(motherduck_module, "duckdb", SimpleNamespace(connect=connect))


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


def test_adapter_rejects_unsupported_options():
    with pytest.raises(SemanticLayerError) as exc:
        MotherDuckAdapter(_options(host="example.com"))
    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'host'" in str(exc.value)


def test_adapter_rejects_literal_token_secret():
    with pytest.raises(SemanticLayerError) as exc:
        MotherDuckAdapter({"token": "md_live_supersecret", "database": "sr_jaffle"})
    assert exc.value.code == "INVALID_CONFIG"
    # The literal secret VALUE must never appear in the error.
    assert "md_live_supersecret" not in str(exc.value)
    assert "md_live_supersecret" not in repr(exc.value.details)


def test_adapter_requires_database_and_token_indirection():
    with pytest.raises(SemanticLayerError) as exc:
        MotherDuckAdapter({"database": "sr_jaffle"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "token_env or token_file" in str(exc.value)

    with pytest.raises(SemanticLayerError) as exc:
        MotherDuckAdapter({"token_env": "SR_MOTHERDUCK_TOKEN"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "database" in str(exc.value)


def test_adapter_reports_missing_token_env(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _fake_duckdb(monkeypatch, captured)
    monkeypatch.delenv("SR_MOTHERDUCK_TOKEN", raising=False)
    adapter = MotherDuckAdapter(_options())

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == ["SR_MOTHERDUCK_TOKEN"]
    assert "connect" not in captured.get("config", {})
    assert "path" not in captured  # never connected


def test_adapter_rejects_non_identifier_database(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _fake_duckdb(monkeypatch, captured)
    monkeypatch.setenv("SR_MOTHERDUCK_TOKEN", "tok-value")
    adapter = MotherDuckAdapter(_options(database="bad name; DROP"))

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["option"] == "database"
    # Option VALUES never leak into the error.
    assert "DROP" not in str(exc.value)
    assert "path" not in captured


def test_adapter_connects_with_token_and_defaults_namespace(monkeypatch: pytest.MonkeyPatch):
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
    assert captured["log"][:4] == [
        'CREATE DATABASE IF NOT EXISTS "sr_jaffle"',
        'USE "sr_jaffle"',
        'CREATE SCHEMA IF NOT EXISTS "analytics"',
        'USE "sr_jaffle"."analytics"',
    ]
    assert "select 1" in captured["log"]

    adapter.close()
    assert captured["conn"].closed is True


def test_adapter_reads_token_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    captured: dict = {}
    _fake_duckdb(monkeypatch, captured)
    monkeypatch.delenv("SR_MOTHERDUCK_TOKEN", raising=False)
    token_file = tmp_path / "md_token"
    token_file.write_text("file-token\n")
    adapter = MotherDuckAdapter({"token_file": str(token_file), "database": "sr_jaffle"})

    adapter.query("select 1")

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


def test_create_adapter_entry_point_via_registry_factory(monkeypatch: pytest.MonkeyPatch):
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


def test_create_adapter_rejects_wrong_kind():
    package = SimpleNamespace(connection=SimpleNamespace(kind="duckdb_native", options={}))
    with pytest.raises(SemanticLayerError) as exc:
        create_adapter(package)
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
        compiled = compile_query(config, registry, case.payload)
        sql = compiled["sql"]
        assert isinstance(sql, str) and sql.strip(), f"{case.name}: empty rendered SQL"
        # MotherDuck IS DuckDB SQL — the rendered text must match the
        # DuckDB reference compilation exactly.
        duck_sql = compile_query(duck_config, duck_registry, case.payload)["sql"]
        assert sql == duck_sql, f"{case.name}: motherduck SQL diverges from duckdb"
