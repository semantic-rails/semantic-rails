"""Offline unit tests for the BigQuery dialect + adapter.

No network: the driver is faked via sys.modules, and the compile sweep
only renders SQL (no warehouse connection is needed to compile).
"""

from __future__ import annotations

import re
import sys
import types
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.bigquery import (
    BigQueryNativeAdapter,
    _bigquery_compat_sql,
    _safe_field_name,
    create_adapter,
)
from semantic_rails.dialects import BigQueryDialect, dialect_for_warehouse, warehouse_connector
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.schema import ConnectionSpec, PackageMeta, SeedSpec
from semantic_rails.sql_ast import SqlBinary, SqlIdentifier, SqlLiteral

DIALECT = BigQueryDialect()


def _col(*parts: str) -> SqlIdentifier:
    return SqlIdentifier(parts=list(parts))


# ---------------------------------------------------------------------------
# (a) Rendering — one assertion per overridden dialect method.
# ---------------------------------------------------------------------------


def test_registry_returns_bigquery_dialect():
    assert isinstance(dialect_for_warehouse("bigquery"), BigQueryDialect)
    connector = warehouse_connector("bigquery")
    assert connector is not None
    assert connector.connection_kinds == ("bigquery_native",)
    assert connector.adapter == "semantic_rails.db_parts.bigquery:create_adapter"


def test_date_trunc_reverses_args_and_uses_datetime_trunc():
    sql = render_expr(DIALECT.date_trunc("day", _col("t", "created_at")))
    assert sql == "DATETIME_TRUNC(t.created_at, DAY)"


def test_date_trunc_week_maps_to_isoweek_for_duckdb_parity():
    sql = render_expr(DIALECT.date_trunc("week", _col("t", "created_at")))
    assert sql == "DATETIME_TRUNC(t.created_at, ISOWEEK)"


@pytest.mark.parametrize("grain", ["month", "quarter", "year", "hour", "minute", "second"])
def test_date_trunc_passes_other_grains_through(grain: str):
    sql = render_expr(DIALECT.date_trunc(grain, _col("t", "ts")))
    assert sql == f"DATETIME_TRUNC(t.ts, {grain.upper()})"


def test_date_diff_puts_end_first():
    sql = render_expr(DIALECT.date_diff("day", _col("s", "started_at"), _col("e", "ended_at")))
    assert sql == "DATETIME_DIFF(e.ended_at, s.started_at, DAY)"


@pytest.mark.parametrize("unit", ["day", "week", "month", "quarter", "year", "hour"])
def test_date_diff_supports_compiler_units(unit: str):
    sql = render_expr(DIALECT.date_diff(unit, _col("a"), _col("b")))
    assert sql == f"DATETIME_DIFF(b, a, {unit.upper()})"


def test_date_add_renders_parenthesized_interval():
    # BigQuery accepts DATETIME_ADD(dt, INTERVAL (7) DAY) — the
    # parenthesized form the SqlInterval node renders.
    sql = render_expr(DIALECT.date_add("day", SqlLiteral(7), _col("t", "d")))
    assert sql == "DATETIME_ADD(t.d, INTERVAL (7) DAY)"


def test_date_add_supports_month_unit():
    sql = render_expr(DIALECT.date_add("month", SqlLiteral(1), _col("t", "d")))
    assert sql == "DATETIME_ADD(t.d, INTERVAL (1) MONTH)"


def _exact_percentile_sql(column: str, p: str) -> str:
    """The exact linear-interpolation form percentile_cont lowers to:
    v[FLOOR(h)] + (h - FLOOR(h)) * (v[CEIL(h)] - v[FLOOR(h)]) with
    h = p * (COUNT(x) - 1) over ARRAY_AGG(x ORDER BY x ASC)."""
    h = f"{p} * (COUNT({column}) - 1)"
    arr = f"(ARRAY_AGG({column} ORDER BY {column} ASC))"
    lo = f"{arr}[OFFSET(CAST(FLOOR({h}) AS BIGINT))]"
    hi = f"{arr}[OFFSET(CAST(CEIL({h}) AS BIGINT))]"
    return f"{lo} + ({h} - FLOOR({h})) * ({hi} - {lo})"


def test_percentile_cont_is_exact_linear_interpolation():
    # APPROX_QUANTILES returns existing elements (no interpolation) and
    # deviated from the DuckDB reference live; the dialect rebuilds the
    # exact QUANTILE_CONT definition from ARRAY_AGG + COUNT instead.
    sql = render_expr(DIALECT.percentile_cont(_col("x"), 0.95))
    assert sql == _exact_percentile_sql("x", "0.95")
    assert "APPROX_QUANTILES" not in sql


def test_percentile_cont_p80_form():
    sql = render_expr(DIALECT.percentile_cont(_col("x"), 0.8))
    assert sql == _exact_percentile_sql("x", "0.8")


def test_median_is_percentile_50():
    sql = render_expr(DIALECT.median(_col("x")))
    assert sql == _exact_percentile_sql("x", "0.5")
    assert sql == render_expr(DIALECT.percentile_cont(_col("x"), 0.5))


def test_first_value_uses_array_agg_order_limit_offset():
    sql = render_expr(DIALECT.first_value(_col("x"), _col("o")))
    assert sql == "(ARRAY_AGG(x ORDER BY o ASC LIMIT 1))[OFFSET(0)]"


def test_last_value_orders_descending():
    sql = render_expr(DIALECT.last_value(_col("x"), _col("o")))
    assert sql == "(ARRAY_AGG(x ORDER BY o DESC LIMIT 1))[OFFSET(0)]"


def test_null_safe_eq_pairs_joinable_equality_with_exact_predicate():
    # BigQuery rejects FULL OUTER JOINs without a plain field-equality
    # conjunct, so null_safe_eq mirrors the Postgres dialect: a text-
    # normalized equality (the conjunct the planner requires) AND the
    # exact IS NOT DISTINCT FROM predicate (the filter).
    sql = render_expr(DIALECT.null_safe_eq(_col("a"), _col("b")))
    assert sql == (
        "COALESCE(CAST(a AS STRING), '__sr_null__') = "
        "COALESCE(CAST(b AS STRING), '__sr_null__') "
        "AND a IS NOT DISTINCT FROM b"
    )


def test_conditional_aggregate_count_uses_native_countif():
    condition = SqlBinary(_col("a"), "=", _col("b"))
    sql = render_expr(DIALECT.conditional_aggregate("count", condition, None))
    assert sql == "COUNTIF(a = b)"


def test_conditional_aggregate_other_shapes_fall_back_to_case():
    condition = SqlBinary(_col("a"), "=", _col("b"))
    sql = render_expr(DIALECT.conditional_aggregate("sum", condition, _col("v")))
    assert sql == "SUM(CASE WHEN a = b THEN v END)"
    counted = render_expr(DIALECT.conditional_aggregate("count", condition, _col("v")))
    assert counted == "COUNT(CASE WHEN a = b THEN v END)"


def test_timestamp_type_is_datetime_and_cast_is_identity():
    assert DIALECT.timestamp_type_name() == "DATETIME"
    # CAST(x AS DATETIME) is not representable in the SQL AST — the
    # dialect relies on BigQuery's implicit DATE/string-literal ->
    # DATETIME coercion instead of emitting a cast.
    expr = _col("t", "ts")
    assert DIALECT.timestamp_cast(expr) is expr


def test_capabilities_flag_exact_percentiles_and_array_agg_nulls():
    caps = DIALECT.capabilities()
    assert caps["timestamp_type"] == "DATETIME"
    assert caps["conditional_aggregate_native"] is True
    assert caps["null_safe_equality_function"] == "IS NOT DISTINCT FROM"
    assert caps["percentile_cont_exact"] is True
    assert caps["median_exact"] is True
    assert "ARRAY_AGG" in caps["percentile_cont_method"]
    # Residual caveat: ARRAY_AGG rejects NULL elements and IGNORE NULLS
    # is not representable in the SQL AST.
    assert "NULL" in caps["percentile_cont_method"]
    assert "NULL" in caps["first_last_value_method"]


# ---------------------------------------------------------------------------
# (b) GoogleSQL compatibility pass — backtick re-quoting + field-name
# legalization (BigQuery rejected both live; see _bigquery_compat_sql).
# ---------------------------------------------------------------------------


def test_compat_sql_requotes_identifiers_with_backticks():
    sql = 'SELECT t.x AS "plain_alias" FROM "jaffle_order" AS t'
    rewritten, alias_map = _bigquery_compat_sql(sql)
    assert rewritten == "SELECT t.x AS `plain_alias` FROM `jaffle_order` AS t"
    assert alias_map == {}  # legal names need no row-key mapping


def test_compat_sql_legalizes_dotted_aliases_consistently():
    # BigQuery field names may not contain '.' (even backticked, even
    # with flexible column names) — every reference must rewrite to the
    # SAME legal name so alias definition and ORDER BY stay consistent.
    sql = (
        'SELECT t.name AS "dimension.jaffle_store_name" FROM jaffle_store AS t '
        'ORDER BY "dimension.jaffle_store_name" ASC'
    )
    rewritten, alias_map = _bigquery_compat_sql(sql)
    assert '"' not in rewritten
    assert "dimension.jaffle_store_name" not in rewritten
    assert len(alias_map) == 1
    safe = next(iter(alias_map))
    assert alias_map[safe] == "dimension.jaffle_store_name"
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", safe)
    assert rewritten.count(f"`{safe}`") == 2


def test_compat_sql_keeps_distinct_illegal_names_distinct():
    sql = 'SELECT 1 AS "measure.a", 2 AS "measure.b"'
    _, alias_map = _bigquery_compat_sql(sql)
    assert sorted(alias_map.values()) == ["measure.a", "measure.b"]
    assert len(set(alias_map)) == 2
    assert _safe_field_name("measure.a") != _safe_field_name("measure.b")


def test_compat_sql_never_touches_string_literals():
    sql = "SELECT 'keep \"this\" verbatim', 'it''s \"fine\"' AS \"metric.x\""
    rewritten, _ = _bigquery_compat_sql(sql)
    assert "'keep \"this\" verbatim'" in rewritten
    assert "'it''s \"fine\"'" in rewritten


def test_safe_field_name_is_deterministic_and_bounded():
    name = "temporal_role.jaffle_order_time__month"
    safe = _safe_field_name(name)
    assert safe == _safe_field_name(name)
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", safe)
    assert len(_safe_field_name("x" * 1000)) <= 300  # BigQuery field limit


# ---------------------------------------------------------------------------
# (c) Adapter — option normalization, secrets, env, redaction.
# ---------------------------------------------------------------------------


def _install_fake_bigquery(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    *,
    rows: list[dict[str, Any]] | None = None,
    fail: Exception | None = None,
) -> None:
    bigquery = types.ModuleType("google.cloud.bigquery")

    class FakeQueryJobConfig:
        def __init__(self) -> None:
            self.default_dataset: Any = None
            self.job_timeout_ms: Any = None

    class FakeRow:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def items(self):
            return self._data.items()

    class FakeJob:
        def __init__(self, payload: list[dict[str, Any]]) -> None:
            self._payload = payload

        def result(self):
            return [FakeRow(row) for row in self._payload]

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs
            self.project = kwargs.get("project", "adc-default-project")

        def query(self, sql: str, job_config: Any = None) -> FakeJob:
            captured["sql"] = sql
            captured["job_config"] = job_config
            if fail is not None:
                raise fail
            return FakeJob(rows or [])

        def close(self) -> None:
            captured["closed"] = True

    bigquery.Client = FakeClient
    bigquery.QueryJobConfig = FakeQueryJobConfig

    service_account = types.ModuleType("google.oauth2.service_account")

    class FakeCredentials:
        @classmethod
        def from_service_account_file(cls, path: str) -> FakeCredentials:
            captured["credentials_path"] = path
            return cls()

    service_account.Credentials = FakeCredentials

    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", types.ModuleType("google.cloud"))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery)
    monkeypatch.setitem(sys.modules, "google.oauth2", types.ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", service_account)


def test_adapter_normalizes_connection_options():
    adapter = BigQueryNativeAdapter(
        {"Project": " demo-project ", "dataset": "jaffle", "location": "EU"}
    )
    assert adapter.options == {
        "project": "demo-project",
        "dataset": "jaffle",
        "location": "EU",
    }
    assert adapter.supports_statement_timeout is True


def test_adapter_rejects_unknown_options():
    with pytest.raises(SemanticLayerError) as exc:
        BigQueryNativeAdapter({"dataset": "jaffle", "compression": "snappy"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'compression'" in str(exc.value)


def test_adapter_rejects_literal_secret_options():
    with pytest.raises(SemanticLayerError) as exc:
        BigQueryNativeAdapter({"credentials": '{"type": "service_account"}'})
    assert exc.value.code == "INVALID_CONFIG"
    assert "credentials" in str(exc.value)
    assert "service_account" not in str(exc.value.details)


def test_adapter_reports_missing_env_without_values(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    _install_fake_bigquery(monkeypatch, captured)
    monkeypatch.delenv("SR_BQ_TEST_PROJECT", raising=False)
    monkeypatch.delenv("SR_BQ_TEST_CREDS", raising=False)
    adapter = BigQueryNativeAdapter(
        {
            "project_env": "SR_BQ_TEST_PROJECT",
            "credentials_file_env": "SR_BQ_TEST_CREDS",
            "dataset": "jaffle",
        }
    )

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == ["SR_BQ_TEST_CREDS", "SR_BQ_TEST_PROJECT"]
    assert exc.value.details["engine"] == "bigquery"
    assert exc.value.details["connection_kind"] == "bigquery_native"
    assert "sql" not in exc.value.details


def test_adapter_query_defaults_dataset_and_maps_rows(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    _install_fake_bigquery(
        monkeypatch,
        captured,
        rows=[{"one": 1, "two": "x"}, {"one": 2, "two": "y"}],
    )
    monkeypatch.setenv("SR_BQ_TEST_PROJECT", "demo-project")
    monkeypatch.setenv("SR_BQ_TEST_CREDS", "/tmp/sa.json")
    adapter = BigQueryNativeAdapter(
        {
            "project_env": "SR_BQ_TEST_PROJECT",
            "credentials_file_env": "SR_BQ_TEST_CREDS",
            "dataset": "jaffle",
        }
    )

    rows = adapter.query("select 1", limits={"max_rows": 1, "statement_timeout_ms": 1500})
    adapter.close()

    assert rows == [{"one": 1, "two": "x"}]  # max_rows clip
    assert captured["sql"] == "select 1"
    assert captured["client_kwargs"]["project"] == "demo-project"
    assert captured["credentials_path"] == "/tmp/sa.json"
    job_config = captured["job_config"]
    assert job_config.default_dataset == "demo-project.jaffle"
    assert job_config.job_timeout_ms == 2000  # 1500ms rounds up to 2s
    assert captured["closed"] is True


def test_adapter_query_maps_safe_aliases_back_to_contract_names(monkeypatch: pytest.MonkeyPatch):
    # The compat pass renames illegal output fields; the adapter must
    # rename result-row keys BACK so callers see the contract aliases.
    captured: dict[str, Any] = {}
    safe = _safe_field_name("dimension.jaffle_store_name")
    _install_fake_bigquery(monkeypatch, captured, rows=[{safe: "Philadelphia", "n": 3}])
    adapter = BigQueryNativeAdapter({"project": "demo-project", "dataset": "jaffle"})

    rows = adapter.query(
        'SELECT name AS "dimension.jaffle_store_name", COUNT(1) AS "n" '
        'FROM jaffle_store GROUP BY "dimension.jaffle_store_name"'
    )

    assert rows == [{"dimension.jaffle_store_name": "Philadelphia", "n": 3}]
    assert '"' not in captured["sql"]
    assert f"`{safe}`" in captured["sql"]
    assert "`n`" in captured["sql"]


def test_adapter_falls_back_to_adc_without_credentials_options(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    _install_fake_bigquery(monkeypatch, captured, rows=[])
    adapter = BigQueryNativeAdapter({"project": "demo-project", "dataset": "jaffle"})

    adapter.query("select 1")

    assert "credentials" not in captured["client_kwargs"]
    assert "credentials_path" not in captured


def test_adapter_redacts_query_errors(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    _install_fake_bigquery(monkeypatch, captured, fail=RuntimeError("boom: " + "x" * 2000))
    adapter = BigQueryNativeAdapter({"project": "demo-project", "dataset": "jaffle"})

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select secret_column from t")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "bigquery"
    assert details["connection_kind"] == "bigquery_native"
    assert details["option_keys"] == ["dataset", "project"]
    assert details["sql_redacted"] is True
    # Option VALUES and raw SQL never leak; driver text is bounded.
    assert "demo-project" not in repr(details)
    assert "secret_column" not in repr(details)
    assert "secret_column" not in str(exc.value)
    assert "[truncated]" in str(exc.value)


def test_adapter_maps_missing_driver_to_missing_dependency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)
    adapter = BigQueryNativeAdapter({"project": "demo-project", "dataset": "jaffle"})

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "MISSING_DEPENDENCY"
    assert "semantic-rails[bigquery]" in str(exc.value)


def test_create_adapter_factory_selects_bigquery_native(monkeypatch: pytest.MonkeyPatch):
    package = PackageMeta(
        package_id="bq_demo",
        name="bq_demo",
        description="bigquery demo",
        warehouse="bigquery",
        connection=ConnectionSpec(
            kind="bigquery_native",
            options={"project_env": "SR_BQ_TEST_PROJECT", "dataset": "jaffle"},
        ),
    )

    adapter = create_warehouse_adapter(package)
    assert isinstance(adapter, BigQueryNativeAdapter)
    assert adapter.options == {"project_env": "SR_BQ_TEST_PROJECT", "dataset": "jaffle"}

    direct = create_adapter(package)
    assert isinstance(direct, BigQueryNativeAdapter)


def test_create_adapter_rejects_other_kinds():
    package = PackageMeta(
        package_id="bq_demo",
        name="bq_demo",
        description="bigquery demo",
        warehouse="bigquery",
        connection=ConnectionSpec(kind="snowflake_cli", options={}),
    )
    with pytest.raises(SemanticLayerError) as exc:
        create_adapter(package)
    assert exc.value.code == "INVALID_CONFIG"


# ---------------------------------------------------------------------------
# (d) Compile-only sweep — every battery case renders non-empty SQL.
# ---------------------------------------------------------------------------


def _bigquery_config(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="bigquery",
            default_db="",
            seed=SeedSpec(),
            connection=ConnectionSpec(
                kind="bigquery_native",
                options={
                    "project_env": "SR_BIGQUERY_PROJECT",
                    "credentials_file_env": "GOOGLE_APPLICATION_CREDENTIALS",
                    "dataset": "jaffle",
                },
            ),
        ),
    )


def test_jaffle_battery_compiles_for_bigquery():
    from tests.integration.harness import load_battery

    config = _bigquery_config(
        load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    )
    registry = Registry(config)

    compiled: dict[str, str] = {}
    for case in load_battery():
        sql = compile_query(config, registry, case.payload)["sql"]
        assert sql and sql.strip(), f"{case.name}: empty rendered SQL"
        compiled[case.name] = sql

    assert len(compiled) >= 15, f"battery shrank to {len(compiled)} cases"

    combined = "\n".join(compiled.values())
    # Portable/duckdb forms must be fully rewritten for BigQuery.
    for forbidden in [
        "QUANTILE_CONT(",
        "MEDIAN(",
        "arg_min(",
        "arg_max(",
        "MIN_BY(",
        "MAX_BY(",
        "DATE_TRUNC(",
        "DATE_DIFF(",
        "DATEDIFF(",
        "DATE_ADD(",
        "DATEADD(",
        "EQUAL_NULL(",
        "PERCENTILE_CONT(",
        "generate_series(",
        "TIMESTAMP_NTZ",
        # Approximate percentiles regressed parity live — the exact
        # ARRAY_AGG interpolation must be emitted instead.
        "APPROX_QUANTILES(",
    ]:
        assert forbidden not in combined, f"BigQuery SQL still contains {forbidden}"
    # No timestamp casts: DATETIME is not castable through the AST and
    # the dialect must not fall back to CAST(... AS TIMESTAMP).
    assert re.search(r"AS TIMESTAMP\)", combined) is None
    # BigQuery-native forms show up where the battery exercises them.
    assert "DATETIME_TRUNC(" in combined
    assert "DATETIME_DIFF(" in combined
    # Exact percentile lowering (ARRAY_AGG interpolation) is exercised
    # by the percentile battery cases.
    assert "ORDER BY" in combined and "[OFFSET(CAST(FLOOR(" in combined
