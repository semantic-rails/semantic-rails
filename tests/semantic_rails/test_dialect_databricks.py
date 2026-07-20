"""Offline unit tests for the Databricks connector.

Three layers, no network:

1. Rendering — every ``DatabricksDialect`` override produces the exact
   Spark SQL form (via ``semantic_rails.renderer.render_expr``).
2. Adapter — option normalization, literal-secret rejection, missing
   env vars, connect kwargs (scheme stripping, namespace defaults),
   statement-timeout hooks, and error redaction against a fake
   ``databricks.sql`` driver in ``sys.modules``.
3. Compile-only sweep — the full conformance battery compiles to
   non-empty SQL with ``warehouse: databricks`` and no DuckDB-only
   function forms leaking through.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace

import pytest

from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.databricks import DatabricksNativeAdapter, create_adapter
from semantic_rails.dialects import (
    DatabricksDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.renderer import render_expr
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, PackageMeta, SeedSpec
from semantic_rails.sql_ast import SqlIdentifier, SqlLiteral

DIALECT = dialect_for_warehouse("databricks")


def _col(*parts: str) -> SqlIdentifier:
    return SqlIdentifier(parts=list(parts))


# ---------------------------------------------------------------------------
# 1. Dialect rendering
# ---------------------------------------------------------------------------


def test_registry_returns_databricks_dialect():
    assert isinstance(DIALECT, DatabricksDialect)
    connector = warehouse_connector("databricks")
    assert connector is not None
    assert connector.connection_kinds == ("databricks_native",)
    assert connector.adapter == "semantic_rails.db_parts.databricks:create_adapter"


def test_date_trunc_keeps_portable_spark_form():
    rendered = render_expr(DIALECT.date_trunc("month", _col("t", "created_at")))
    assert rendered == "DATE_TRUNC('month', CAST(t.created_at AS TIMESTAMP))"


def test_date_trunc_week_stays_week_token():
    # Spark `date_trunc('week', …)` truncates to Monday — ISO parity
    # with the DuckDB reference, so the grain passes through unchanged.
    rendered = render_expr(DIALECT.date_trunc("week", _col("t", "created_at")))
    assert rendered == "DATE_TRUNC('week', CAST(t.created_at AS TIMESTAMP))"


@pytest.mark.parametrize("unit", ["second", "minute", "hour", "day", "month", "quarter", "year"])
def test_date_diff_renders_boundary_aligned_timestampdiff(unit: str):
    # Boundary-crossing parity with DuckDB: both operands are truncated
    # to the unit BEFORE TIMESTAMPDIFF, so Spark's complete-elapsed-unit
    # counting equals boundary counting (verified live: 23:00 -> 01:00
    # next day must be 1 day, not 0).
    rendered = render_expr(DIALECT.date_diff(unit, _col("s"), _col("e")))
    trunc = lambda operand: f"DATE_TRUNC('{unit}', CAST({operand} AS TIMESTAMP))"  # noqa: E731
    assert rendered == f"TIMESTAMPDIFF({unit.upper()}, {trunc('s')}, {trunc('e')})"


@pytest.mark.parametrize("unit", ["week", "isoweek"])
def test_date_diff_week_is_truncated_day_delta_over_seven(unit: str):
    # DuckDB's date_diff('week', ...) is NOT a Monday-boundary count:
    # it is the day delta divided by 7, truncated toward zero (same
    # relowering as the Postgres dialect). Spark / is float division,
    # so the quotient is CAST to BIGINT (truncates toward zero).
    rendered = render_expr(DIALECT.date_diff(unit, _col("s"), _col("e")))
    trunc = lambda operand: f"DATE_TRUNC('day', CAST({operand} AS TIMESTAMP))"  # noqa: E731
    assert rendered == (f"CAST(TIMESTAMPDIFF(DAY, {trunc('s')}, {trunc('e')}) / 7 AS BIGINT)")


@pytest.mark.parametrize(
    "unit", ["second", "minute", "hour", "day", "week", "month", "quarter", "year"]
)
def test_date_add_renders_timestampadd_with_unit_keyword(unit: str):
    rendered = render_expr(DIALECT.date_add(unit, SqlLiteral(3), _col("t", "ts")))
    assert rendered == f"TIMESTAMPADD({unit.upper()}, 3, t.ts)"


def test_percentile_cont_renders_within_group():
    rendered = render_expr(DIALECT.percentile_cont(_col("t", "amount"), 0.95))
    assert rendered == "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY t.amount ASC)"


def test_median_renders_native_median():
    rendered = render_expr(DIALECT.median(_col("t", "amount")))
    assert rendered == "MEDIAN(t.amount)"


def test_first_and_last_value_render_min_by_max_by():
    first = render_expr(DIALECT.first_value(_col("t", "x"), _col("t", "ts")))
    last = render_expr(DIALECT.last_value(_col("t", "x"), _col("t", "ts")))
    assert first == "MIN_BY(t.x, t.ts)"
    assert last == "MAX_BY(t.x, t.ts)"


def test_null_safe_eq_renders_spaceship_operator():
    rendered = render_expr(DIALECT.null_safe_eq(_col("a", "k"), _col("b", "k")))
    assert rendered == "(a.k <=> b.k)"


def test_conditional_aggregate_count_renders_count_if():
    condition = DIALECT.null_safe_eq(_col("t", "status"), SqlLiteral("paid"))
    rendered = render_expr(DIALECT.conditional_aggregate("count", condition, None))
    assert rendered == "COUNT_IF((t.status <=> 'paid'))"


def test_conditional_aggregate_sum_keeps_portable_case_form():
    from semantic_rails.sql_ast import SqlBinary

    condition = SqlBinary(_col("t", "status"), "=", SqlLiteral("paid"))
    rendered = render_expr(DIALECT.conditional_aggregate("sum", condition, _col("t", "amount")))
    assert rendered == "SUM(CASE WHEN t.status = 'paid' THEN t.amount END)"


def test_timestamp_type_is_plain_timestamp():
    assert DIALECT.timestamp_type_name() == "TIMESTAMP"
    assert render_expr(DIALECT.timestamp_cast(_col("t", "ts"))) == "CAST(t.ts AS TIMESTAMP)"


def test_convert_timezone_keeps_portable_form():
    # Databricks SQL has convert_timezone(sourceTz, targetTz, ts) with the
    # same argument order as the portable default.
    rendered = render_expr(DIALECT.convert_timezone("UTC", "America/New_York", _col("t", "ts")))
    assert rendered == "CONVERT_TIMEZONE('UTC', 'America/New_York', t.ts)"


# ---------------------------------------------------------------------------
# 2. Adapter
# ---------------------------------------------------------------------------


def _install_fake_driver(monkeypatch: pytest.MonkeyPatch, captured: dict, *, raise_text: str = ""):
    class FakeCursor:
        description = [("ONE",), ("TWO",)]

        def execute(self, sql):
            captured.setdefault("statements", []).append(sql)
            if raise_text:
                raise RuntimeError(raise_text)

        def fetchall(self):
            return [(1, "x")]

        def close(self):
            captured["cursor_closed"] = True

    class FakeConnection:
        def __init__(self, **kwargs):
            captured["connect_kwargs"] = kwargs

        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["connection_closed"] = True

    sql_module = types.ModuleType("databricks.sql")
    sql_module.connect = lambda **kwargs: FakeConnection(**kwargs)
    databricks_module = types.ModuleType("databricks")
    databricks_module.sql = sql_module
    monkeypatch.setitem(sys.modules, "databricks", databricks_module)
    monkeypatch.setitem(sys.modules, "databricks.sql", sql_module)


def _adapter_options(**overrides) -> dict:
    options = {
        "host_env": "SR_TEST_DBX_HOST",
        "http_path_env": "SR_TEST_DBX_HTTP_PATH",
        "token_env": "SR_TEST_DBX_TOKEN",
        "catalog": "sr_catalog",
        "schema": "sr_schema",
    }
    options.update(overrides)
    return options


def _set_connection_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SR_TEST_DBX_HOST", "https://adb-123.4.azuredatabricks.net/")
    monkeypatch.setenv("SR_TEST_DBX_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.setenv("SR_TEST_DBX_TOKEN", "dapi-super-secret")


def test_adapter_normalizes_connection_options():
    adapter = DatabricksNativeAdapter({"HOST-ENV": " SR_TEST_DBX_HOST ", "catalog": " main "})
    assert adapter.options == {"host_env": "SR_TEST_DBX_HOST", "catalog": "main"}


def test_adapter_rejects_literal_secret_options():
    with pytest.raises(SemanticLayerError) as exc:
        DatabricksNativeAdapter(_adapter_options(token="dapi-literal-secret"))
    assert exc.value.code == "INVALID_CONFIG"
    assert "dapi-literal-secret" not in str(exc.value)


def test_adapter_rejects_unsupported_options():
    with pytest.raises(SemanticLayerError) as exc:
        DatabricksNativeAdapter(_adapter_options(warehouse_id="abc"))
    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'warehouse_id'" in str(exc.value)


def test_adapter_reports_missing_env_without_leaking_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SR_TEST_DBX_HOST", "adb-123.4.azuredatabricks.net")
    monkeypatch.setenv("SR_TEST_DBX_TOKEN", "dapi-super-secret")
    monkeypatch.delenv("SR_TEST_DBX_HTTP_PATH", raising=False)

    adapter = DatabricksNativeAdapter(_adapter_options())
    with pytest.raises(SemanticLayerError) as exc:
        adapter._connect_kwargs()

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == ["SR_TEST_DBX_HTTP_PATH"]
    assert "dapi-super-secret" not in str(exc.value)
    assert "dapi-super-secret" not in repr(exc.value.details)


def test_adapter_requires_host_http_path_and_token():
    adapter = DatabricksNativeAdapter({"catalog": "main"})
    with pytest.raises(SemanticLayerError) as exc:
        adapter._connect_kwargs()
    assert exc.value.code == "INVALID_CONFIG"
    message = str(exc.value)
    assert "host" in message and "http_path" in message and "token" in message


def test_adapter_connects_with_namespace_and_stripped_host(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    _set_connection_env(monkeypatch)

    adapter = DatabricksNativeAdapter(_adapter_options())
    rows = adapter.query("select 1")
    adapter.close()

    assert rows == [{"ONE": 1, "TWO": "x"}]
    kwargs = captured["connect_kwargs"]
    # https:// scheme and trailing slash stripped from server_hostname.
    assert kwargs["server_hostname"] == "adb-123.4.azuredatabricks.net"
    assert kwargs["http_path"] == "/sql/1.0/warehouses/abc123"
    assert kwargs["access_token"] == "dapi-super-secret"
    # Namespace defaults from connection options so unqualified table
    # names (jaffle_order, …) resolve.
    assert kwargs["catalog"] == "sr_catalog"
    assert kwargs["schema"] == "sr_schema"
    assert captured["statements"] == ["select 1"]
    assert captured["cursor_closed"] is True
    assert captured["connection_closed"] is True


def test_adapter_applies_and_resets_statement_timeout(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    _set_connection_env(monkeypatch)

    adapter = DatabricksNativeAdapter(_adapter_options())
    assert adapter.supports_statement_timeout is True
    adapter.query("select 1", limits={"statement_timeout_ms": 4500})

    assert captured["statements"] == [
        "SET STATEMENT_TIMEOUT = 5",
        "select 1",
        "RESET STATEMENT_TIMEOUT",
    ]


def test_query_rewrites_double_quoted_identifiers_to_backticks(monkeypatch: pytest.MonkeyPatch):
    # Spark SQL parses "double quoted" tokens as STRING LITERALS, so the
    # renderer's ANSI-quoted dot-aliases (AS "dimension.x") would select
    # a constant. The adapter's compat pass re-quotes them with
    # backticks before execution — and never touches single-quoted
    # string literals (including '' escapes and embedded double quotes).
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    _set_connection_env(monkeypatch)

    adapter = DatabricksNativeAdapter(_adapter_options())
    adapter.query(
        'SELECT t.product_type AS "dimension.jaffle_product_type", '
        "'it''s a \"literal\"' AS note FROM jaffle_order t"
    )

    assert captured["statements"] == [
        "SELECT t.product_type AS `dimension.jaffle_product_type`, "
        "'it''s a \"literal\"' AS note FROM jaffle_order t"
    ]


def test_query_floats_nullif_divisions(monkeypatch: pytest.MonkeyPatch):
    # Spark DECIMAL / DECIMAL keeps DECIMAL with a REDUCED result scale
    # (a DECIMAL revenue share came back as 0.064117 live), so ratio
    # metrics lose precision vs DuckDB's float division. The compat
    # pass casts the compiler's NULLIF divide-by-zero guard to DOUBLE,
    # recursively for nested ratios, skipping string literals.
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    _set_connection_env(monkeypatch)

    adapter = DatabricksNativeAdapter(_adapter_options())
    adapter.query("SELECT a / NULLIF(b / NULLIF(c, 0), 0) AS r, ' / NULLIF(' AS note FROM t")

    assert captured["statements"] == [
        "SELECT a / CAST(NULLIF(b / CAST(NULLIF(c, 0) AS DOUBLE), 0) AS DOUBLE) AS r, "
        "' / NULLIF(' AS note FROM t"
    ]


def test_adapter_redacts_driver_errors(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured, raise_text="TABLE_OR_VIEW_NOT_FOUND: boom")
    _set_connection_env(monkeypatch)

    adapter = DatabricksNativeAdapter(_adapter_options())
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select secret_col from jaffle_order")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "databricks"
    assert details["connection_kind"] == "databricks_native"
    assert details["option_keys"] == sorted(adapter.options)
    assert details["sql_redacted"] is True
    # Never raw SQL or option VALUES in the envelope.
    assert "sql" not in details
    assert "jaffle_order" not in repr(details)
    assert "dapi-super-secret" not in str(exc.value)
    assert "dapi-super-secret" not in repr(details)
    # Bounded driver text is allowed in the message.
    assert "TABLE_OR_VIEW_NOT_FOUND" in str(exc.value)


def test_adapter_maps_missing_driver_to_missing_dependency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "databricks", None)
    monkeypatch.setitem(sys.modules, "databricks.sql", None)
    _set_connection_env(monkeypatch)

    adapter = DatabricksNativeAdapter(_adapter_options())
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "MISSING_DEPENDENCY"
    assert "semantic-rails[databricks]" in str(exc.value)


def _databricks_package(kind: str = "databricks_native") -> PackageMeta:
    return PackageMeta(
        package_id="databricks_demo",
        name="databricks_demo",
        description="databricks demo",
        warehouse="databricks",
        connection=ConnectionSpec(kind=kind, options=_adapter_options()),
    )


def test_create_warehouse_adapter_selects_databricks_native():
    adapter = create_warehouse_adapter(_databricks_package())
    assert isinstance(adapter, DatabricksNativeAdapter)
    assert adapter.options["host_env"] == "SR_TEST_DBX_HOST"
    assert adapter.options["catalog"] == "sr_catalog"


def test_create_adapter_rejects_unknown_connection_kind():
    with pytest.raises(SemanticLayerError) as exc:
        create_adapter(_databricks_package(kind="databricks_cli"))
    assert exc.value.code == "INVALID_CONFIG"
    assert "databricks_cli" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. Compile-only battery sweep
# ---------------------------------------------------------------------------


def _databricks_config():
    from semantic_rails.config import load_package_config
    from tests.integration.harness import PACKAGE_DIR
    from tests.integration.targets.databricks import TARGET

    config = load_package_config(str(PACKAGE_DIR))
    package = replace(
        config.package,
        warehouse="databricks",
        default_db="",
        seed=SeedSpec(),
        connection=ConnectionSpec(
            kind=TARGET.connection_kind,
            name="",
            options=dict(TARGET.connection_options),
        ),
    )
    return replace(config, package=package), PACKAGE_DIR


def test_databricks_compiles_full_battery_offline():
    from tests.integration.harness import load_battery

    config, package_dir = _databricks_config()
    runtime = Runtime.from_config(
        config, source_path=str(package_dir), package_id=config.package.package_id
    )
    battery = load_battery()
    assert len(battery) >= 15, f"conformance battery shrank to {len(battery)} cases"

    for case in battery:
        result = runtime.compile(dict(case.payload))
        assert result.get("ok"), f"{case.name}: compile failed — {result.get('errors')}"
        sql = str(result.get("rendered_sql") or "")
        assert sql.strip(), f"{case.name}: empty rendered SQL"
        for forbidden in (
            "QUANTILE_CONT(",
            "arg_min(",
            "arg_max(",
            "IS NOT DISTINCT FROM",
            "DATE_DIFF(",
            "DATEDIFF(",
            "DATEADD(",
            "DATE_ADD(",
            "EQUAL_NULL(",
            "TIMESTAMP_NTZ",
        ):
            assert forbidden not in sql, f"{case.name}: '{forbidden}' leaked into databricks SQL"
