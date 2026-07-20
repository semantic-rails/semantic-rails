"""Offline unit tests for the ClickHouse connector.

Covers (a) dialect rendering for every overridden method, (b) adapter
option normalization / secret hygiene / error redaction with a faked
``clickhouse_connect`` driver, and (c) a compile-only sweep of the full
integration battery against the clickhouse dialect. No network.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace

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
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_ast import SqlIdentifier

DIALECT = ClickHouseDialect()


def _ident(name: str) -> SqlIdentifier:
    return SqlIdentifier(parts=[name])


# ---------------------------------------------------------------------------
# (a) Dialect rendering — one assertion per overridden method.
# ---------------------------------------------------------------------------


def test_registry_returns_clickhouse_dialect():
    assert isinstance(dialect_for_warehouse("clickhouse"), ClickHouseDialect)
    connector = warehouse_connector("clickhouse")
    assert connector is not None
    assert connector.connection_kinds == ("clickhouse_native",)
    assert connector.adapter == "semantic_rails.db_parts.clickhouse:create_adapter"


def test_percentile_cont_renders_parameterized_quantile():
    rendered = render_expr(DIALECT.percentile_cont(_ident("x"), 0.95))
    assert rendered == "quantileExactInclusive(0.95)(x)"


def test_median_renders_quantile_half():
    rendered = render_expr(DIALECT.median(_ident("x")))
    assert rendered == "quantileExactInclusive(0.5)(x)"


def test_first_and_last_value_render_argmin_argmax():
    assert render_expr(DIALECT.first_value(_ident("x"), _ident("ts"))) == "argMin(x, ts)"
    assert render_expr(DIALECT.last_value(_ident("x"), _ident("ts"))) == "argMax(x, ts)"


def test_date_trunc_recasts_result_to_timestamp():
    # ClickHouse date_trunc returns Date for week-and-coarser grains —
    # the outer cast restores timestamp parity with DuckDB.
    rendered = render_expr(DIALECT.date_trunc("month", _ident("ts")))
    assert rendered == "CAST(DATE_TRUNC('month', CAST(ts AS TIMESTAMP)) AS TIMESTAMP)"


def test_null_safe_eq_renders_spaceship_operator():
    rendered = render_expr(DIALECT.null_safe_eq(_ident("a"), _ident("b")))
    assert "<=>" in rendered
    assert rendered.replace("(", "").replace(")", "") == "a <=> b"


def test_portable_defaults_kept_for_date_diff_date_add_and_conditional_aggregate():
    # Verified live on ClickHouse 24.8: DATE_DIFF('unit', s, e) and
    # DATE_ADD(date, INTERVAL n UNIT) aliases work with DuckDB
    # (boundary-crossing, Monday-week) semantics — so no override.
    diff = render_expr(DIALECT.date_diff("day", _ident("s"), _ident("e")))
    assert diff == "DATE_DIFF('day', CAST(s AS TIMESTAMP), CAST(e AS TIMESTAMP))"
    from semantic_rails.sql_ast import SqlLiteral

    add = render_expr(DIALECT.date_add("day", SqlLiteral(3), _ident("d")))
    assert add == "DATE_ADD(d, INTERVAL (3) DAY)"
    agg = render_expr(DIALECT.conditional_aggregate("count", _ident("cond"), None))
    assert agg == "COUNT(CASE WHEN cond THEN 1 END)"


def test_window_lag_emulates_lag_with_argmin_over_bounded_frame():
    # ClickHouse 24.x has no LAG window function; window_lag builds the
    # exact-equivalent CASE/COUNT/argMin form (verified live against
    # lagInFrame, including partitions and NULL leading rows).
    from semantic_rails.sql_ast import SqlOrderTerm

    rendered = render_expr(
        DIALECT.window_lag(
            _ident("m1"),
            12,
            partition_by=[_ident("g1")],
            order_by=[SqlOrderTerm(expr=_ident("t"), direction="ASC")],
        )
    )
    assert rendered == (
        "CASE WHEN COUNT(1) OVER (PARTITION BY g1 ORDER BY t ASC "
        "ROWS BETWEEN 12 PRECEDING AND CURRENT ROW) = 13 "
        "THEN argMin(m1, t) OVER (PARTITION BY g1 ORDER BY t ASC "
        "ROWS BETWEEN 12 PRECEDING AND CURRENT ROW) END"
    )


def test_capabilities_reflect_clickhouse_reality():
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
    class FakeClient:
        def __init__(self, **kwargs):
            captured["connect_kwargs"] = kwargs

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
    monkeypatch.setitem(sys.modules, "clickhouse_connect", module)


def _adapter_options() -> dict[str, str]:
    return {
        "host_env": "SR_CH_TEST_HOST",
        "port": "9000",
        "database": "sr_jaffle",
        "user_env": "SR_CH_TEST_USER",
        "password_env": "SR_CH_TEST_PASSWORD",
    }


def test_adapter_normalizes_connection_options():
    adapter = ClickHouseAdapter({"HOST-ENV": "SR_CH_TEST_HOST", "database": " sr_jaffle "})
    assert adapter.options == {"host_env": "SR_CH_TEST_HOST", "database": "sr_jaffle"}
    assert adapter.supports_statement_timeout is True


def test_adapter_rejects_literal_secret_options():
    with pytest.raises(SemanticLayerError) as exc:
        ClickHouseAdapter({"password": "hunter2"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "password_env" in str(exc.value)
    assert "hunter2" not in str(exc.value)


def test_adapter_rejects_unsupported_options():
    with pytest.raises(SemanticLayerError) as exc:
        ClickHouseAdapter({"warehouse_size": "XL"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'warehouse_size'" in str(exc.value)


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
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    monkeypatch.setenv("SR_CH_TEST_HOST", "ch.example.com")
    monkeypatch.setenv("SR_CH_TEST_USER", "svc_user")
    monkeypatch.setenv("SR_CH_TEST_PASSWORD", "pw")

    adapter = ClickHouseAdapter(_adapter_options())
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
    assert captured["settings"] is None  # no limits -> no per-query settings
    assert captured["closed"] is True


def test_adapter_applies_statement_timeout_and_row_clip(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured)
    monkeypatch.setenv("SR_CH_TEST_HOST", "ch.example.com")
    monkeypatch.setenv("SR_CH_TEST_USER", "svc_user")
    monkeypatch.setenv("SR_CH_TEST_PASSWORD", "pw")

    adapter = ClickHouseAdapter(_adapter_options())
    rows = adapter.query("select 1", limits={"statement_timeout_ms": 2500, "max_rows": 1})

    assert captured["settings"] == {"max_execution_time": 3}
    assert rows == [{"one": 1, "two": "x"}]


def test_adapter_redacts_query_errors(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    _install_fake_driver(monkeypatch, captured, fail="boom " + "x" * 600)
    monkeypatch.setenv("SR_CH_TEST_HOST", "ch.example.com")
    monkeypatch.setenv("SR_CH_TEST_USER", "svc_user")
    monkeypatch.setenv("SR_CH_TEST_PASSWORD", "super-secret")

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
    # Bounded driver text, never option values or the raw SQL.
    assert "[truncated]" in str(exc.value)
    assert "super-secret" not in str(exc.value)
    assert "secret_table" not in repr(details)


def test_create_warehouse_adapter_selects_clickhouse(monkeypatch: pytest.MonkeyPatch):
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
    for case in cases:
        result = compile_query(config, registry, case.payload)
        sql = result["sql"]
        assert isinstance(sql, str) and sql.strip(), f"{case.name}: empty SQL"
        compiled[case.name] = sql

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
