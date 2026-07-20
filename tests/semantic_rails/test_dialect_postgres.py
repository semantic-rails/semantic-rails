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
from semantic_rails.db_parts.postgres import PostgresAdapter, create_adapter
from semantic_rails.dialects import (
    PostgresDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
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
# Dialect rendering — one assertion per overridden method.
# ---------------------------------------------------------------------------


def test_registry_wires_postgres_dialect_and_adapter():
    connector = warehouse_connector("postgres")
    assert connector is not None
    assert isinstance(connector.dialect, PostgresDialect)
    assert connector.connection_kinds == ("postgres_native",)
    assert connector.adapter == "semantic_rails.db_parts.postgres:create_adapter"
    assert isinstance(dialect_for_warehouse("postgres"), PostgresDialect)


def test_percentile_cont_renders_within_group():
    assert (
        render_expr(DIALECT.percentile_cont(X, 0.95))
        == "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY t.x ASC)"
    )


def test_median_renders_percentile_cont_half():
    assert render_expr(DIALECT.median(X)) == "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.x ASC)"


def test_first_value_renders_ordered_array_agg_subscript():
    assert (
        render_expr(DIALECT.first_value(X, ORDER))
        == "(ARRAY_AGG(t.x ORDER BY t.snapshot_at ASC))[1]"
    )


def test_last_value_renders_descending_array_agg_subscript():
    assert (
        render_expr(DIALECT.last_value(X, ORDER))
        == "(ARRAY_AGG(t.x ORDER BY t.snapshot_at DESC))[1]"
    )


def test_date_diff_day_renders_date_subtraction():
    assert (
        render_expr(DIALECT.date_diff("day", START, END))
        == f"CAST({END_TS} AS DATE) - CAST({START_TS} AS DATE)"
    )


def test_date_diff_week_renders_truncating_day_division():
    # DuckDB week diff is day-diff / 7 truncated toward zero — PG
    # integer division matches exactly (verified live vs DuckDB).
    assert (
        render_expr(DIALECT.date_diff("week", START, END))
        == f"(CAST({END_TS} AS DATE) - CAST({START_TS} AS DATE)) / 7"
    )


def test_date_diff_month_renders_calendar_field_arithmetic():
    assert render_expr(DIALECT.date_diff("month", START, END)) == (
        f"CAST((DATE_PART('year', {END_TS}) - DATE_PART('year', {START_TS})) * 12"
        f" + (DATE_PART('month', {END_TS}) - DATE_PART('month', {START_TS})) AS BIGINT)"
    )


def test_date_diff_quarter_renders_calendar_field_arithmetic():
    assert render_expr(DIALECT.date_diff("quarter", START, END)) == (
        f"CAST((DATE_PART('year', {END_TS}) - DATE_PART('year', {START_TS})) * 4"
        f" + (DATE_PART('quarter', {END_TS}) - DATE_PART('quarter', {START_TS})) AS BIGINT)"
    )


def test_date_diff_year_renders_year_part_delta():
    assert render_expr(DIALECT.date_diff("year", START, END)) == (
        f"CAST(DATE_PART('year', {END_TS}) - DATE_PART('year', {START_TS}) AS BIGINT)"
    )


@pytest.mark.parametrize(
    ("unit", "divisor"),
    [("hour", 3600), ("minute", 60)],
)
def test_date_diff_subdaily_renders_truncated_epoch_delta(unit: str, divisor: int):
    assert render_expr(DIALECT.date_diff(unit, START, END)) == (
        f"CAST((DATE_PART('epoch', DATE_TRUNC('{unit}', {END_TS}))"
        f" - DATE_PART('epoch', DATE_TRUNC('{unit}', {START_TS}))) / {divisor} AS BIGINT)"
    )


def test_date_diff_second_renders_epoch_delta():
    assert render_expr(DIALECT.date_diff("second", START, END)) == (
        f"CAST(DATE_PART('epoch', DATE_TRUNC('second', {END_TS}))"
        f" - DATE_PART('epoch', DATE_TRUNC('second', {START_TS})) AS BIGINT)"
    )


def test_date_diff_never_emits_datediff_functions():
    for unit in ("second", "minute", "hour", "day", "week", "month", "quarter", "year"):
        sql = render_expr(DIALECT.date_diff(unit, START, END))
        assert "DATE_DIFF(" not in sql and "DATEDIFF(" not in sql, (unit, sql)


@pytest.mark.parametrize(
    ("unit", "upper", "lower"),
    [
        ("day", "2000-01-02", "2000-01-01"),
        ("week", "2000-01-08", "2000-01-01"),
        ("hour", "2000-01-01 01:00:00", "2000-01-01 00:00:00"),
        ("minute", "2000-01-01 00:01:00", "2000-01-01 00:00:00"),
        ("second", "2000-01-01 00:00:01", "2000-01-01 00:00:00"),
    ],
)
def test_date_add_fixed_units_render_scaled_timestamp_difference(unit: str, upper: str, lower: str):
    assert render_expr(DIALECT.date_add(unit, SqlLiteral(3), X)) == (
        "CAST(t.x AS TIMESTAMP) + 3 * "
        f"(CAST('{upper}' AS TIMESTAMP) - CAST('{lower}' AS TIMESTAMP))"
    )


@pytest.mark.parametrize("unit", ["month", "quarter", "year"])
def test_date_add_month_family_renders_clamped_calendar_reconstruction(unit: str):
    sql = render_expr(DIALECT.date_add(unit, SqlLiteral(2), X))
    # Target month rebuilt as text (PG renders no INTERVAL literal
    # through this AST) with day-of-month clamping for short months.
    assert "CONCAT(" in sql
    assert "'-01') AS TIMESTAMP)" in sql
    assert "CASE WHEN DATE_PART('day', CAST(t.x AS TIMESTAMP)) <=" in sql
    assert "FLOOR(" in sql


def test_date_add_never_emits_interval_keyword():
    # `INTERVAL (n) UNIT` is a Postgres syntax error (the parenthesized
    # form is a precision spec), so no PG lowering may contain it.
    for unit in ("second", "minute", "hour", "day", "week", "month", "quarter", "year"):
        sql = render_expr(DIALECT.date_add(unit, SqlLiteral(2), X))
        assert "INTERVAL" not in sql, (unit, sql)
        assert "DATE_ADD(" not in sql and "DATEADD(" not in sql, (unit, sql)


def test_null_safe_eq_pairs_hash_joinable_clause_with_exact_predicate():
    # IS NOT DISTINCT FROM alone is rejected by PG's FULL JOIN planner
    # ("only supported with merge-joinable or hash-joinable join
    # conditions"); the leading text-normalized equality is the
    # joinable clause, the trailing predicate keeps exact semantics.
    assert render_expr(DIALECT.null_safe_eq(X, ORDER)) == (
        "COALESCE(CAST(t.x AS TEXT), '__sr_null__')"
        " = COALESCE(CAST(t.snapshot_at AS TEXT), '__sr_null__')"
        " AND t.x IS NOT DISTINCT FROM t.snapshot_at"
    )


def test_conditional_aggregate_keeps_portable_case_form():
    condition = SqlBinary(X, ">", SqlLiteral(0))
    assert (
        render_expr(DIALECT.conditional_aggregate("sum", condition, ORDER))
        == "SUM(CASE WHEN t.x > 0 THEN t.snapshot_at END)"
    )
    assert (
        render_expr(DIALECT.conditional_aggregate("count", condition, None))
        == "COUNT(CASE WHEN t.x > 0 THEN 1 END)"
    )


def test_timestamp_type_is_plain_timestamp():
    assert DIALECT.timestamp_type_name() == "TIMESTAMP"
    assert render_expr(DIALECT.timestamp_cast(X)) == "CAST(t.x AS TIMESTAMP)"


def test_capabilities_advertise_postgres_features():
    capabilities = DIALECT.capabilities()
    assert capabilities["percentile_cont"] is True
    assert capabilities["null_safe_equality_function"] == "IS NOT DISTINCT FROM"
    assert capabilities["timestamp_type"] == "TIMESTAMP"
    assert capabilities["qualify"] is False


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


def _install_fake_psycopg(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    class FakeCursor:
        description = [("one",), ("two",)]

        def execute(self, sql):
            captured.setdefault("sql", []).append(sql)
            raiser = captured.get("raise_on")
            if raiser and raiser in sql:
                raise RuntimeError("connection reset by peer")

        def fetchall(self):
            return [(1, "x")]

        def close(self):
            captured["cursor_closed"] = True

    class FakeConnection:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["connection_closed"] = True

    module = types.ModuleType("psycopg")
    module.connect = lambda **kwargs: FakeConnection(**kwargs)
    monkeypatch.setitem(sys.modules, "psycopg", module)


def test_adapter_normalizes_options_and_builds_connect_kwargs(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    adapter = PostgresAdapter(
        {
            "HOST-ENV": "SR_PG_TEST_HOST",
            **{key: value for key, value in VALID_OPTIONS.items() if key != "host_env"},
        }
    )

    assert adapter.options == VALID_OPTIONS
    kwargs = adapter._connect_kwargs()
    assert kwargs == {
        "port": 5433,
        "autocommit": True,
        "host": "pg.example.test",
        "user": "svc_user",
        "password": "super-secret",
        "dbname": "sr_jaffle",
        "options": "-c search_path=analytics",
    }


def test_adapter_defaults_port_and_includes_sslmode_and_session_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_pg_env(monkeypatch)
    options = {key: value for key, value in VALID_OPTIONS.items() if key != "port"}
    options.update({"sslmode": "require", "statement_timeout_seconds": "30"})
    kwargs = PostgresAdapter(options)._connect_kwargs()

    assert kwargs["port"] == 5432
    assert kwargs["sslmode"] == "require"
    assert kwargs["options"] == "-c search_path=analytics -c statement_timeout=30000"


def test_adapter_rejects_unsupported_option():
    with pytest.raises(SemanticLayerError) as exc:
        PostgresAdapter({**VALID_OPTIONS, "warehouse": "COMPUTE_WH"})

    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'warehouse'" in str(exc.value)


def test_adapter_rejects_literal_password_without_leaking_it():
    with pytest.raises(SemanticLayerError) as exc:
        PostgresAdapter({**VALID_OPTIONS, "password": "hunter2"})

    assert exc.value.code == "INVALID_CONFIG"
    assert "password" in str(exc.value)
    assert "hunter2" not in str(exc.value)
    assert "hunter2" not in repr(exc.value.details)


def test_adapter_rejects_non_integer_port(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    with pytest.raises(SemanticLayerError) as exc:
        PostgresAdapter({**VALID_OPTIONS, "port": "fivefourthreetwo"})._connect_kwargs()

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["option"] == "port"
    # Option VALUES never appear in error envelopes.
    assert "fivefourthreetwo" not in str(exc.value)


def test_adapter_reports_every_missing_env_var_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
):
    for name in ("SR_PG_TEST_HOST", "SR_PG_TEST_USER", "SR_PG_TEST_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    adapter = PostgresAdapter(VALID_OPTIONS)

    with pytest.raises(SemanticLayerError) as exc:
        adapter._connect_kwargs()

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == [
        "SR_PG_TEST_HOST",
        "SR_PG_TEST_PASSWORD",
        "SR_PG_TEST_USER",
    ]
    assert exc.value.details["engine"] == "postgres"
    assert exc.value.details["connection_kind"] == "postgres_native"


def test_adapter_queries_with_fake_driver_and_maps_rows(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    captured: dict = {}
    _install_fake_psycopg(monkeypatch, captured)
    adapter = PostgresAdapter(VALID_OPTIONS)

    rows = adapter.query("select 1")
    adapter.close()

    assert rows == [{"one": 1, "two": "x"}]
    assert captured["sql"] == ["select 1"]
    assert captured["kwargs"]["autocommit"] is True
    assert captured["kwargs"]["options"] == "-c search_path=analytics"
    assert captured["cursor_closed"] is True
    assert captured["connection_closed"] is True


def test_adapter_applies_and_resets_statement_timeout(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    captured: dict = {}
    _install_fake_psycopg(monkeypatch, captured)
    adapter = PostgresAdapter(VALID_OPTIONS)
    assert adapter.supports_statement_timeout is True

    adapter.query("select 1", limits={"statement_timeout_ms": 1500})

    # 1500 ms rounds up to 2 s at the limits layer, re-expressed in ms
    # for SET statement_timeout, and reset after the statement.
    assert captured["sql"] == [
        "SET statement_timeout = 2000",
        "select 1",
        "RESET statement_timeout",
    ]


def test_adapter_redacts_query_errors(monkeypatch: pytest.MonkeyPatch):
    _set_pg_env(monkeypatch)
    captured: dict = {"raise_on": "jaffle"}
    _install_fake_psycopg(monkeypatch, captured)
    adapter = PostgresAdapter(VALID_OPTIONS)

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
    monkeypatch.setitem(sys.modules, "psycopg", None)
    adapter = PostgresAdapter(VALID_OPTIONS)

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

    assert isinstance(adapter, PostgresAdapter)
    assert adapter.options == VALID_OPTIONS


def test_create_adapter_rejects_unknown_connection_kind():
    with pytest.raises(SemanticLayerError) as exc:
        create_adapter(_package("postgres_cli"))

    assert exc.value.code == "INVALID_CONFIG"
    assert "postgres_cli" in str(exc.value)


# ---------------------------------------------------------------------------
# Adapter SQL compatibility pass (identifier length, float division).
# ---------------------------------------------------------------------------


def test_compat_pass_shortens_colliding_long_identifiers():
    from semantic_rails.db_parts.postgres import _postgres_compat_sql

    name_a = "conversion_leaf_1__" + "a" * 50 + "_first"
    name_b = "conversion_leaf_1__" + "a" * 50 + "_second"
    assert len(name_a) > 63 and name_a[:63] == name_b[:63]  # would collide raw
    sql = (
        f"WITH {name_a} AS (SELECT 1 AS v), {name_b} AS (SELECT 2 AS v)\n"
        f"SELECT {name_a}.v FROM {name_a} CROSS JOIN {name_b}"
    )
    rewritten = _postgres_compat_sql(sql)

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


def test_compat_pass_keeps_short_identifiers_and_literals_untouched():
    from semantic_rails.db_parts.postgres import _postgres_compat_sql

    long_in_literal = "x" * 80
    sql = f"SELECT name FROM jaffle_order WHERE name = '{long_in_literal}'"
    assert _postgres_compat_sql(sql) == sql


def test_compat_pass_casts_nullif_divisions_to_float():
    from semantic_rails.db_parts.postgres import _postgres_compat_sql

    sql = "SELECT a / NULLIF(b, 0) AS r FROM t"
    assert (
        _postgres_compat_sql(sql) == "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE PRECISION) AS r FROM t"
    )


def test_compat_pass_handles_nested_nullif_divisions_and_literals():
    from semantic_rails.db_parts.postgres import _postgres_compat_sql

    sql = "SELECT a / NULLIF(b / NULLIF(c, 0), 0), ') / NULLIF(' FROM t"
    assert _postgres_compat_sql(sql) == (
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
        sql = compile_query(config, registry, case.payload)["sql"]
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
