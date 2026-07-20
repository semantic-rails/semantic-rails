"""Offline unit tests for the Athena (Trino SQL) connector.

No network: the PyAthena driver and boto3 are monkeypatched into
``sys.modules`` (mirroring how test_warehouse_adapters.py fakes
``snowflake.connector``), and the battery sweep only compiles SQL.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.athena import AthenaAdapter, create_adapter
from semantic_rails.dialects import (
    AthenaDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.renderer import render_expr
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_ast import SqlBinary, SqlIdentifier, SqlLiteral

DIALECT = AthenaDialect()


def _ident(*parts: str) -> SqlIdentifier:
    return SqlIdentifier(parts=list(parts))


# ---------------------------------------------------------------------------
# (a) Dialect rendering — every override, plus the inherited Trino-correct
#     defaults the connector relies on.
# ---------------------------------------------------------------------------


def test_athena_registry_entry_uses_athena_dialect():
    connector = warehouse_connector("athena")
    assert connector is not None
    assert isinstance(connector.dialect, AthenaDialect)
    assert connector.connection_kinds == ("athena_native",)
    assert connector.adapter == "semantic_rails.db_parts.athena:create_adapter"
    assert isinstance(dialect_for_warehouse("athena"), AthenaDialect)


def _expected_exact_percentile(p: float, x: str = "x") -> str:
    # Exact linear interpolation over a sorted array (Trino arrays are
    # 1-based; COUNT skips NULLs and ASC sorts NULLS LAST, so positions
    # 1..n are exactly the sorted non-NULL values). Validated live on
    # Athena engine v3 against DuckDB QUANTILE_CONT (incl. NULL-mixed,
    # all-NULL, single-element, and p=0/p=1 inputs).
    pos = f"{p} * (COUNT({x}) - 1)"
    lo = f"(ARRAY_AGG({x} ORDER BY {x} ASC))[CAST(FLOOR({pos}) AS BIGINT) + 1]"
    hi = f"(ARRAY_AGG({x} ORDER BY {x} ASC))[CAST(CEIL({pos}) AS BIGINT) + 1]"
    return f"CASE WHEN COUNT({x}) > 0 THEN {lo} + ({hi} - {lo}) * ({pos} - FLOOR({pos})) END"


def test_athena_percentile_cont_renders_exact_array_interpolation():
    rendered = render_expr(DIALECT.percentile_cont(_ident("x"), 0.95))
    assert rendered == _expected_exact_percentile(0.95)
    # The t-digest sketch must be gone — exactness is the contract.
    assert "APPROX_PERCENTILE" not in rendered


def test_athena_median_renders_exact_percentile_half():
    rendered = render_expr(DIALECT.median(_ident("x")))
    assert rendered == _expected_exact_percentile(0.5)
    assert "APPROX_PERCENTILE" not in rendered


def test_athena_percentile_guards_empty_input_with_case():
    # Empty (or all-NULL) input must yield NULL, not an arr[0] index
    # error: floor(p * (0 - 1)) + 1 == 0 and Trino arrays are 1-based.
    rendered = render_expr(DIALECT.percentile_cont(_ident("x"), 0.8))
    assert rendered.startswith("CASE WHEN COUNT(x) > 0 THEN ")
    assert rendered.endswith(" END")


def test_athena_first_value_renders_min_by():
    rendered = render_expr(DIALECT.first_value(_ident("x"), _ident("t")))
    assert rendered == "MIN_BY(x, t)"


def test_athena_last_value_renders_max_by():
    rendered = render_expr(DIALECT.last_value(_ident("x"), _ident("t")))
    assert rendered == "MAX_BY(x, t)"


def test_athena_date_add_renders_trino_unit_literal_form():
    # Trino: DATE_ADD('unit', value, ts) — NOT the INTERVAL form.
    rendered = render_expr(DIALECT.date_add("day", SqlLiteral(7), _ident("ts")))
    assert rendered == "DATE_ADD('day', 7, ts)"
    assert "INTERVAL" not in rendered


def test_athena_date_add_keeps_unit_lowercase_for_all_compiler_units():
    # Units the compiler emits (expressions.py window/offset units).
    for unit in ("minute", "hour", "day", "week", "month", "quarter", "year"):
        rendered = render_expr(DIALECT.date_add(unit, SqlLiteral(1), _ident("ts")))
        assert rendered == f"DATE_ADD('{unit}', 1, ts)"


def test_athena_date_diff_is_trino_form_with_lowercase_unit():
    # The portable default IS Trino's form — verify it stays that way
    # and the unit literal arrives lowercase ('day', not 'DAY').
    for unit in ("minute", "hour", "day", "week", "month", "quarter", "year"):
        rendered = render_expr(DIALECT.date_diff(unit, _ident("s"), _ident("e")))
        assert rendered == (f"DATE_DIFF('{unit}', CAST(s AS TIMESTAMP), CAST(e AS TIMESTAMP))")


def test_athena_date_trunc_is_portable_trino_form():
    rendered = render_expr(DIALECT.date_trunc("month", _ident("ts")))
    assert rendered == "DATE_TRUNC('month', CAST(ts AS TIMESTAMP))"


def test_athena_null_safe_eq_uses_is_not_distinct_from():
    expr = DIALECT.null_safe_eq(_ident("a"), _ident("b"))
    assert isinstance(expr, SqlBinary)
    assert render_expr(expr) == "a IS NOT DISTINCT FROM b"


def test_athena_conditional_aggregate_uses_portable_case_form():
    condition = SqlBinary(_ident("x"), ">", SqlLiteral(1))
    assert (
        render_expr(DIALECT.conditional_aggregate("count", condition, None))
        == "COUNT(CASE WHEN x > 1 THEN 1 END)"
    )
    assert (
        render_expr(DIALECT.conditional_aggregate("sum", condition, _ident("v")))
        == "SUM(CASE WHEN x > 1 THEN v END)"
    )


def test_athena_timestamp_type_is_plain_timestamp():
    assert DIALECT.timestamp_type_name() == "TIMESTAMP"


def test_athena_capabilities_document_exact_percentiles():
    capabilities = DIALECT.capabilities()
    assert capabilities["percentile_exact"] is True
    assert capabilities["median_exact"] is True
    assert "ARRAY_AGG" in capabilities["percentile_cont_method"]
    assert "percentile_approximation" not in capabilities
    assert capabilities["qualify"] is False
    assert capabilities["timestamp_type"] == "TIMESTAMP"


# ---------------------------------------------------------------------------
# (b) Adapter — fake pyathena driver, option/secret/env contracts,
#     error redaction.
# ---------------------------------------------------------------------------


class _FakeCursor:
    description = [("ONE",), ("TWO",)]

    def __init__(self, log: dict[str, Any], fail: Exception | None = None):
        self._log = log
        self._fail = fail

    def execute(self, sql: str) -> None:
        if self._fail is not None:
            raise self._fail
        self._log["sql"] = sql

    def fetchall(self):
        return [(1, "x")]

    def close(self) -> None:
        self._log["cursor_closed"] = True


class _FakeConnection:
    def __init__(self, log: dict[str, Any], fail: Exception | None = None):
        self._log = log
        self._fail = fail

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._log, self._fail)

    def close(self) -> None:
        self._log["connection_closed"] = True


def _install_fake_pyathena(
    monkeypatch: pytest.MonkeyPatch, log: dict[str, Any], fail: Exception | None = None
) -> None:
    module = types.ModuleType("pyathena")

    def _connect(**kwargs: Any) -> _FakeConnection:
        log["connect_kwargs"] = kwargs
        return _FakeConnection(log, fail)

    module.connect = _connect
    monkeypatch.setitem(sys.modules, "pyathena", module)


def test_athena_adapter_normalizes_options_and_disables_statement_timeout():
    adapter = AthenaAdapter(
        {"Region-Env": " AWS_REGION ", "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR"}
    )
    assert adapter.options == {
        "region_env": "AWS_REGION",
        "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR",
    }
    assert adapter.supports_statement_timeout is False
    assert adapter.engine == "athena"
    assert adapter.connection_kind == "athena_native"


def test_athena_adapter_rejects_unsupported_options():
    with pytest.raises(SemanticLayerError) as exc:
        AthenaAdapter({"host": "example.com"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'host'" in str(exc.value)


@pytest.mark.parametrize(
    "secret_key", ["password", "token", "secret", "api_key", "credentials", "private_key"]
)
def test_athena_adapter_rejects_literal_secret_options(secret_key: str):
    # AWS credentials must come from the ambient boto3 chain — any
    # literal credential-looking option is rejected outright.
    with pytest.raises(SemanticLayerError) as exc:
        AthenaAdapter({secret_key: "hunter2"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "hunter2" not in str(exc.value)


def test_athena_adapter_reports_missing_env_without_values(monkeypatch: pytest.MonkeyPatch):
    log: dict[str, Any] = {}
    _install_fake_pyathena(monkeypatch, log)
    monkeypatch.delenv("SR_TEST_ATHENA_REGION", raising=False)
    monkeypatch.delenv("SR_TEST_ATHENA_STAGING", raising=False)
    adapter = AthenaAdapter(
        {
            "region_env": "SR_TEST_ATHENA_REGION",
            "s3_staging_dir_env": "SR_TEST_ATHENA_STAGING",
        }
    )

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == [
        "SR_TEST_ATHENA_REGION",
        "SR_TEST_ATHENA_STAGING",
    ]
    assert "connect_kwargs" not in log


def test_athena_adapter_requires_region_and_staging_dir(monkeypatch: pytest.MonkeyPatch):
    log: dict[str, Any] = {}
    _install_fake_pyathena(monkeypatch, log)
    adapter = AthenaAdapter({"database": "sr_jaffle"})

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_options"] == ["region", "s3_staging_dir"]


def test_athena_adapter_connects_with_env_indirection_and_defaults_namespace(
    monkeypatch: pytest.MonkeyPatch,
):
    log: dict[str, Any] = {}
    _install_fake_pyathena(monkeypatch, log)
    monkeypatch.setenv("SR_TEST_ATHENA_REGION", "eu-central-1")
    monkeypatch.setenv("SR_TEST_ATHENA_STAGING", "s3://sr-bucket/athena-results/")
    adapter = AthenaAdapter(
        {
            "region_env": "SR_TEST_ATHENA_REGION",
            "s3_staging_dir_env": "SR_TEST_ATHENA_STAGING",
            "database": "sr_jaffle",
            "workgroup": "primary",
        }
    )

    rows = adapter.query("select 1")
    adapter.close()

    assert rows == [{"ONE": 1, "TWO": "x"}]
    assert log["sql"] == "select 1"
    assert log["connect_kwargs"] == {
        "s3_staging_dir": "s3://sr-bucket/athena-results/",
        "region_name": "eu-central-1",
        # Namespace defaults from connection options so unqualified
        # table names (jaffle_order, …) resolve.
        "schema_name": "sr_jaffle",
        "work_group": "primary",
    }
    assert log["cursor_closed"] is True
    assert log["connection_closed"] is True


def test_athena_adapter_defaults_schema_and_omits_workgroup(monkeypatch: pytest.MonkeyPatch):
    log: dict[str, Any] = {}
    _install_fake_pyathena(monkeypatch, log)
    adapter = AthenaAdapter({"region": "us-east-1", "s3_staging_dir": "s3://sr-bucket/results"})

    adapter.query("select 1")

    assert log["connect_kwargs"]["schema_name"] == "default"
    assert log["connect_kwargs"]["work_group"] is None


def test_athena_adapter_redacts_driver_errors(monkeypatch: pytest.MonkeyPatch):
    log: dict[str, Any] = {}
    _install_fake_pyathena(
        monkeypatch, log, fail=RuntimeError("SYNTAX_ERROR: line 1:8 mismatched input")
    )
    adapter = AthenaAdapter(
        {"region": "us-east-1", "s3_staging_dir": "s3://sr-bucket/results", "database": "db"}
    )

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("SELECT super_secret_column FROM jaffle_order")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "athena"
    assert details["connection_kind"] == "athena_native"
    assert details["option_keys"] == ["database", "region", "s3_staging_dir"]
    assert details["sql_redacted"] is True
    # Never option VALUES, never the raw SQL.
    assert "sql" not in details
    assert "us-east-1" not in repr(details)
    assert "super_secret_column" not in repr(details)
    # Bounded driver text still reaches the message for authors.
    assert "SYNTAX_ERROR" in str(exc.value)


def test_create_warehouse_adapter_selects_athena(monkeypatch: pytest.MonkeyPatch):
    package = PackageMeta(
        package_id="athena_demo",
        name="athena_demo",
        description="athena demo",
        warehouse="athena",
        connection=ConnectionSpec(
            kind="athena_native",
            options={
                "region_env": "AWS_REGION",
                "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR",
                "database": "sr_jaffle",
            },
        ),
    )

    adapter = create_warehouse_adapter(package)

    assert isinstance(adapter, AthenaAdapter)
    assert adapter.options["database"] == "sr_jaffle"
    assert create_adapter(package).options == adapter.options


# ---------------------------------------------------------------------------
# (b1) Compatibility pass — Trino integer/decimal division truncates, so
#      the compiler's `x / NULLIF(y, 0)` ratio guard is cast to DOUBLE
#      before execution (mirrors db_parts/postgres.py).
# ---------------------------------------------------------------------------


def test_athena_compat_casts_nullif_ratio_guard_to_double():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "SELECT a / NULLIF(b, 0) AS ratio FROM t"
    assert _athena_compat_sql(sql) == "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) AS ratio FROM t"


def test_athena_compat_rewrites_nested_nullif_divisions():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "SELECT x / NULLIF(y / NULLIF(z, 0), 0) FROM t"
    assert _athena_compat_sql(sql) == (
        "SELECT x / CAST(NULLIF(y / CAST(NULLIF(z, 0) AS DOUBLE), 0) AS DOUBLE) FROM t"
    )


def test_athena_compat_skips_string_literals():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "SELECT 'a / NULLIF(b, 0)' AS doc, c / NULLIF(d, 0) FROM t"
    assert _athena_compat_sql(sql) == (
        "SELECT 'a / NULLIF(b, 0)' AS doc, c / CAST(NULLIF(d, 0) AS DOUBLE) FROM t"
    )


def test_athena_compat_handles_quotes_and_parens_inside_guard():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "SELECT a / NULLIF(SUM(CASE WHEN s = ')''(' THEN n END), 0) FROM t"
    assert _athena_compat_sql(sql) == (
        "SELECT a / CAST(NULLIF(SUM(CASE WHEN s = ')''(' THEN n END), 0) AS DOUBLE) FROM t"
    )


def test_athena_compat_leaves_unbalanced_tail_untouched():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "SELECT a / NULLIF(b, 0"  # malformed — never rewrite blindly
    assert _athena_compat_sql(sql) == sql


def test_athena_compat_leaves_plain_division_alone():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "SELECT a / b, NULLIF(c, 0) FROM t"
    assert _athena_compat_sql(sql) == sql


def test_athena_compat_tags_temporal_comparison_literals_as_timestamp():
    # Trino refuses `timestamp >= varchar` (DuckDB coerces), so the
    # compiler's time-window bounds gain an explicit TIMESTAMP tag.
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "WHERE t.ordered_at >= '2016-09-01'\n  AND t.ordered_at < '2017-01-01'"
    assert _athena_compat_sql(sql) == (
        "WHERE t.ordered_at >= TIMESTAMP '2016-09-01'\n  AND t.ordered_at < TIMESTAMP '2017-01-01'"
    )


def test_athena_compat_tags_full_timestamp_literals_and_all_operators():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    for op in ("=", "!=", "<>", "<", ">", "<=", ">="):
        assert _athena_compat_sql(f"WHERE ts {op} '2016-09-01 12:30:00.123'") == (
            f"WHERE ts {op} TIMESTAMP '2016-09-01 12:30:00.123'"
        )


def test_athena_compat_leaves_non_temporal_and_tagged_literals_alone():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    for sql in (
        "WHERE name = 'Brooklyn' AND code = '2016-09'",  # not ISO dates
        "WHERE ts >= TIMESTAMP '2016-09-01'",  # already tagged
        "SELECT '2016-09-01' AS day_label FROM t",  # no comparison op
        "WHERE batch = '2016-09-011'",  # longer than an ISO date
    ):
        assert _athena_compat_sql(sql) == sql


def test_athena_compat_skips_comparisons_inside_string_literals():
    from semantic_rails.db_parts.athena import _athena_compat_sql

    sql = "SELECT 'note: x >= ''2016-09-01''' AS doc, ts < '2016-10-01' FROM t"
    assert _athena_compat_sql(sql) == (
        "SELECT 'note: x >= ''2016-09-01''' AS doc, ts < TIMESTAMP '2016-10-01' FROM t"
    )


def test_athena_adapter_query_applies_compat_pass(monkeypatch: pytest.MonkeyPatch):
    log: dict[str, Any] = {}
    _install_fake_pyathena(monkeypatch, log)
    adapter = AthenaAdapter({"region": "us-east-1", "s3_staging_dir": "s3://sr-bucket/results"})

    adapter.query("SELECT a / NULLIF(b, 0) FROM t WHERE ts >= '2016-09-01'")

    assert log["sql"] == (
        "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) FROM t WHERE ts >= TIMESTAMP '2016-09-01'"
    )


# ---------------------------------------------------------------------------
# (b2) Loader — fake adapter + fake boto3; verify upload + Hive DDL.
# ---------------------------------------------------------------------------


class _FakeLoaderAdapter:
    engine = "athena"
    connection_kind = "athena_native"

    def __init__(self, options: dict[str, str], marker_rows: list[dict[str, Any]] | None = None):
        self.options = options
        self.executed: list[str] = []
        self._marker_rows = marker_rows

    def query(self, sql: str, *, limits: dict[str, Any] | None = None):
        self.executed.append(sql)
        if sql.strip().upper().startswith("SELECT FINGERPRINT"):
            if self._marker_rows is None:
                raise RuntimeError("marker table missing")
            return self._marker_rows
        return []

    def close(self) -> None:
        return None


def _install_fake_boto3(monkeypatch: pytest.MonkeyPatch, uploads: list[tuple[str, str, str]]):
    module = types.ModuleType("boto3")

    class _FakeS3Client:
        def __init__(self, region_name: str = ""):
            self.region_name = region_name

        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            uploads.append((filename, bucket, key))

    def _client(service: str, region_name: str = "", **kwargs: Any) -> _FakeS3Client:
        assert service == "s3"
        module.last_region = region_name
        return _FakeS3Client(region_name)

    module.client = _client
    monkeypatch.setitem(sys.modules, "boto3", module)
    return module


def _fake_fixture(tmp_path):
    from tests.integration.fixture import FixtureColumn, FixtureTable, JaffleFixture

    table = FixtureTable(
        name="jaffle_order",
        columns=(
            FixtureColumn(name="order_id", duckdb_type="BIGINT", logical_type="integer"),
            FixtureColumn(name="amount", duckdb_type="DECIMAL(18,3)", logical_type="decimal"),
            FixtureColumn(name="ratio", duckdb_type="DOUBLE", logical_type="float"),
            FixtureColumn(name="store", duckdb_type="VARCHAR", logical_type="string"),
            FixtureColumn(name="day", duckdb_type="DATE", logical_type="date"),
            FixtureColumn(name="at", duckdb_type="TIMESTAMP", logical_type="timestamp"),
            FixtureColumn(name="ok", duckdb_type="BOOLEAN", logical_type="boolean"),
        ),
        row_count=3,
    )
    (tmp_path / "jaffle_order.parquet").write_bytes(b"PAR1fake")
    return JaffleFixture(path=tmp_path, fingerprint="fp123", tables=(table,))


def test_athena_loader_uploads_parquet_and_creates_external_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    from tests.integration.loaders.athena import AthenaFixtureLoader

    uploads: list[tuple[str, str, str]] = []
    boto3_module = _install_fake_boto3(monkeypatch, uploads)
    monkeypatch.setenv("SR_TEST_ATHENA_STAGING", "s3://sr-bucket/athena/results/")
    adapter = _FakeLoaderAdapter(
        {
            "region": "us-east-1",
            "s3_staging_dir_env": "SR_TEST_ATHENA_STAGING",
            "database": "sr_jaffle",
        }
    )
    fixture = _fake_fixture(tmp_path)
    loader = AthenaFixtureLoader(adapter)

    loader.ensure_loaded(fixture)

    assert uploads == [
        (
            str(tmp_path / "jaffle_order.parquet"),
            "sr-bucket",
            "athena/results/sr_fixture/fp123/jaffle_order/part.parquet",
        )
    ]
    assert boto3_module.last_region == "us-east-1"
    assert "CREATE DATABASE IF NOT EXISTS sr_jaffle" in adapter.executed
    assert "DROP TABLE IF EXISTS jaffle_order" in adapter.executed
    create_sql = next(sql for sql in adapter.executed if sql.startswith("CREATE EXTERNAL TABLE"))
    assert create_sql == (
        "CREATE EXTERNAL TABLE jaffle_order ("
        "order_id bigint, amount decimal(18,3), ratio double, store string, "
        "day date, at timestamp, ok boolean) "
        "STORED AS PARQUET "
        "LOCATION 's3://sr-bucket/athena/results/sr_fixture/fp123/jaffle_order/'"
    )
    # Marker is a CTAS — Athena has no plain CREATE TABLE + INSERT.
    assert "DROP TABLE IF EXISTS sr_fixture_meta" in adapter.executed
    assert "CREATE TABLE sr_fixture_meta AS SELECT 'fp123' AS fingerprint" in adapter.executed
    assert not any(sql.lstrip().upper().startswith("INSERT") for sql in adapter.executed)


def test_athena_loader_skips_load_when_marker_matches(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from tests.integration.loaders.athena import AthenaFixtureLoader

    uploads: list[tuple[str, str, str]] = []
    _install_fake_boto3(monkeypatch, uploads)
    adapter = _FakeLoaderAdapter(
        {"region": "us-east-1", "s3_staging_dir": "s3://sr-bucket/results"},
        marker_rows=[{"fingerprint": "fp123"}],
    )
    fixture = _fake_fixture(tmp_path)

    AthenaFixtureLoader(adapter).ensure_loaded(fixture)

    assert uploads == []
    assert adapter.executed == ["SELECT fingerprint FROM sr_fixture_meta"]


def test_athena_target_uses_env_indirection_and_ambient_aws_creds():
    from tests.integration.targets.athena import TARGET

    assert TARGET.warehouse == "athena"
    assert TARGET.connection_kind == "athena_native"
    assert TARGET.connection_options["region_env"] == "AWS_REGION"
    assert TARGET.connection_options["s3_staging_dir_env"] == "SR_ATHENA_S3_STAGING_DIR"
    assert TARGET.connection_options["database"]
    # Credentials are ambient (boto3 chain) — required env, never options.
    for name in (
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SR_ATHENA_S3_STAGING_DIR",
        "SR_ATHENA_DATABASE",
    ):
        assert name in TARGET.required_env
    assert "AWS_ACCESS_KEY_ID" not in str(TARGET.connection_options)
    assert TARGET.make_loader is not None


# ---------------------------------------------------------------------------
# (c) Compile-only sweep — the full battery compiles for warehouse=athena
#     with non-empty, Trino-safe SQL. No warehouse connection involved.
# ---------------------------------------------------------------------------


def _athena_config(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="athena",
            default_db="",
            seed=replace(config.package.seed, kind="", source="", post_sql=""),
            connection=ConnectionSpec(
                kind="athena_native",
                options={
                    "region_env": "AWS_REGION",
                    "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR",
                    "database": "sr_jaffle",
                },
            ),
        ),
    )


def test_athena_compiles_entire_battery_to_non_empty_sql():
    from semantic_rails.config import load_package_config
    from tests.integration.harness import PACKAGE_DIR, load_battery

    config = load_package_config(str(PACKAGE_DIR))
    runtime = Runtime.from_config(
        _athena_config(config),
        source_path=str(PACKAGE_DIR),
        package_id=config.package.package_id,
    )
    cases = load_battery()
    assert cases, "battery is empty — harness misconfigured"
    for case in cases:
        compiled = runtime.compile(case.payload)
        sql = str(compiled.get("rendered_sql") or "")
        assert sql.strip(), f"{case.name}: compiled to empty rendered SQL"
        # DuckDB-only forms must never leak into Athena SQL, and the
        # approximate percentile sketch is retired in favor of the
        # exact ARRAY_AGG interpolation.
        for forbidden in (
            "QUANTILE_CONT(",
            "MEDIAN(",
            "arg_min(",
            "arg_max(",
            "APPROX_PERCENTILE(",
        ):
            assert forbidden not in sql, f"{case.name}: forbidden {forbidden} in SQL"
        # Trino rejects the portable INTERVAL date_add form.
        assert "INTERVAL (" not in sql, f"{case.name}: INTERVAL form leaked into SQL"
