"""Offline tests for the DuckLake warehouse connector.

Three layers, no network and no live warehouse:

- dialect rendering: DuckLake IS DuckDB SQL — the dialect subclasses
  DuckDbDialect with zero overrides, and every portable rendering hook
  must keep emitting the DuckDB forms verbatim;
- adapter: option normalization, secret/unknown-option rejection,
  missing-env errors, redacted error envelopes, and the exact
  INSTALL/LOAD/ATTACH/USE bootstrap (driver faked via sys.modules);
- compile-only sweep: the full conformance battery compiles for
  warehouse=ducklake without any connection.
"""

from __future__ import annotations

import os
import sys
import types
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, repo_root, resolve_repo_path
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.ducklake import DuckLakeAdapter, create_adapter
from semantic_rails.dialects import (
    DuckDbDialect,
    DuckLakeDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_ast import SqlIdentifier, SqlLiteral

# ---------------------------------------------------------------------------
# Dialect rendering — DuckLake must render exactly like DuckDB.
# ---------------------------------------------------------------------------

X = SqlIdentifier(["x"])
Y = SqlIdentifier(["y"])


def _dialect() -> DuckLakeDialect:
    dialect = dialect_for_warehouse("ducklake")
    assert isinstance(dialect, DuckLakeDialect)
    return dialect


def test_ducklake_dialect_is_duckdb_subclass_with_no_overrides():
    dialect = _dialect()
    assert isinstance(dialect, DuckDbDialect)
    assert dialect.name == "ducklake"
    # Contract: DuckLake is DuckDB SQL over an attached catalog — the
    # dialect class must not override any rendering hook.
    overridden = set(DuckLakeDialect.__dict__) & {
        name for name in dir(DuckDbDialect) if not name.startswith("__")
    }
    assert overridden - {"name"} == set()


def test_ducklake_percentile_and_median_render_duckdb_forms():
    dialect = _dialect()
    assert render_expr(dialect.percentile_cont(X, 0.95)) == "QUANTILE_CONT(x, 0.95)"
    assert render_expr(dialect.median(X)) == "MEDIAN(x)"


def test_ducklake_first_last_value_render_arg_min_max():
    dialect = _dialect()
    assert render_expr(dialect.first_value(X, Y)) == "arg_min(x, y)"
    assert render_expr(dialect.last_value(X, Y)) == "arg_max(x, y)"


def test_ducklake_date_functions_render_duckdb_forms():
    dialect = _dialect()
    assert render_expr(dialect.date_trunc("week", X)) == "DATE_TRUNC('week', CAST(x AS TIMESTAMP))"
    assert (
        render_expr(dialect.date_diff("day", X, Y))
        == "DATE_DIFF('day', CAST(x AS TIMESTAMP), CAST(y AS TIMESTAMP))"
    )
    # Cover the units the compiler actually emits (windows / offsets /
    # conversion lookbacks): boundary-crossing semantics are DuckDB's own.
    for unit in ("day", "week", "month", "quarter", "year", "hour", "minute", "second"):
        assert render_expr(dialect.date_diff(unit, X, Y)).startswith(f"DATE_DIFF('{unit}'")
        assert render_expr(dialect.date_add(unit, SqlLiteral(3), X)) == (
            f"DATE_ADD(x, INTERVAL (3) {unit.upper()})"
        )


def test_ducklake_null_safe_eq_and_conditional_aggregate_render_portable_forms():
    dialect = _dialect()
    assert render_expr(dialect.null_safe_eq(X, Y)) == "x IS NOT DISTINCT FROM y"
    condition = dialect.null_safe_eq(X, Y)
    assert (
        render_expr(dialect.conditional_aggregate("count", condition, None))
        == "COUNT(CASE WHEN x IS NOT DISTINCT FROM y THEN 1 END)"
    )
    assert (
        render_expr(dialect.conditional_aggregate("sum", condition, Y))
        == "SUM(CASE WHEN x IS NOT DISTINCT FROM y THEN y END)"
    )


def test_ducklake_timestamp_and_capabilities_match_duckdb():
    dialect = _dialect()
    assert dialect.timestamp_type_name() == "TIMESTAMP"
    assert render_expr(dialect.timestamp_cast(X)) == "CAST(x AS TIMESTAMP)"
    assert dialect.capabilities() == DuckDbDialect().capabilities()


def test_ducklake_registry_entry():
    connector = warehouse_connector("ducklake")
    assert connector is not None
    assert connector.connection_kinds == ("ducklake_native",)
    assert connector.adapter == "semantic_rails.db_parts.ducklake:create_adapter"
    for option in ("catalog_path", "catalog_path_env", "data_path", "data_path_env", "schema"):
        assert option in connector.connection_options


# ---------------------------------------------------------------------------
# Adapter — driver faked via sys.modules; no real duckdb connection.
# ---------------------------------------------------------------------------


class FakeCursor:
    description = [("ONE",), ("TWO",)]

    def __init__(self, log: dict[str, Any]):
        self._log = log

    def execute(self, sql: str) -> None:
        self._log.setdefault("cursor_sql", []).append(sql)
        if self._log.get("fail_query") and not sql.startswith("USE "):
            raise RuntimeError("boom: relation jaffle_order does not exist")

    def fetchall(self):
        return [(1, "x")]

    def close(self) -> None:
        self._log["cursor_closed"] = True


class FakeConnection:
    def __init__(self, log: dict[str, Any]):
        self._log = log

    def execute(self, sql: str) -> None:
        self._log.setdefault("setup_sql", []).append(sql)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._log)

    def close(self) -> None:
        self._log["connection_closed"] = True


def _install_fake_duckdb(monkeypatch: pytest.MonkeyPatch, log: dict[str, Any]) -> None:
    module = types.ModuleType("duckdb")
    module.connect = lambda *args, **kwargs: FakeConnection(log)
    monkeypatch.setitem(sys.modules, "duckdb", module)


def test_adapter_normalizes_options():
    adapter = DuckLakeAdapter(
        {"Catalog-Path": " /tmp/cat.ducklake ", "data_path_env": "SR_DUCKLAKE_DATA_PATH"}
    )
    assert adapter.options == {
        "catalog_path": "/tmp/cat.ducklake",
        "data_path_env": "SR_DUCKLAKE_DATA_PATH",
    }


def test_adapter_rejects_unknown_and_literal_secret_options():
    with pytest.raises(SemanticLayerError) as exc:
        DuckLakeAdapter({"token_env": "SOME_TOKEN"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'token_env'" in str(exc.value)

    # Literal secret keys are forbidden everywhere, DuckLake included.
    with pytest.raises(SemanticLayerError) as exc:
        DuckLakeAdapter({"password": "hunter2"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "hunter2" not in str(exc.value)


def test_adapter_reports_missing_env_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
):
    log: dict[str, Any] = {}
    _install_fake_duckdb(monkeypatch, log)
    monkeypatch.delenv("SR_DUCKLAKE_CATALOG_PATH", raising=False)
    monkeypatch.delenv("SR_DUCKLAKE_DATA_PATH", raising=False)
    adapter = DuckLakeAdapter(
        {
            "catalog_path_env": "SR_DUCKLAKE_CATALOG_PATH",
            "data_path_env": "SR_DUCKLAKE_DATA_PATH",
        }
    )

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == [
        "SR_DUCKLAKE_CATALOG_PATH",
        "SR_DUCKLAKE_DATA_PATH",
    ]
    assert "setup_sql" not in log  # never connected


def test_adapter_requires_catalog_path():
    adapter = DuckLakeAdapter({"schema": "main"})
    with pytest.raises(SemanticLayerError) as exc:
        adapter._resolve_paths()
    assert exc.value.code == "INVALID_CONFIG"
    assert "catalog_path" in str(exc.value)


def test_adapter_bootstrap_attaches_and_uses_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path):
    log: dict[str, Any] = {}
    _install_fake_duckdb(monkeypatch, log)
    catalog = tmp_path / "lake" / "catalog.ducklake"
    data_dir = tmp_path / "lake" / "files"
    monkeypatch.setenv("SR_DUCKLAKE_CATALOG_PATH", str(catalog))
    adapter = DuckLakeAdapter(
        {
            "catalog_path_env": "SR_DUCKLAKE_CATALOG_PATH",
            "data_path": str(data_dir),
        }
    )

    rows = adapter.query("select 1")
    adapter.close()

    assert rows == [{"ONE": 1, "TWO": "x"}]
    assert log["setup_sql"] == [
        "INSTALL ducklake",
        "LOAD ducklake",
        f"ATTACH IF NOT EXISTS 'ducklake:{catalog}' AS jaffle (DATA_PATH '{data_dir}')",
        "USE jaffle",
    ]
    # duckdb cursors are connection duplicates that reset the current
    # catalog to memory.main — the namespace must be re-pinned on EVERY
    # cursor, or queries (and the fixture loader) silently land in the
    # in-memory catalog instead of the lake.
    assert log["cursor_sql"] == ["USE jaffle", "select 1"]
    assert log["cursor_closed"] is True
    assert log["connection_closed"] is True
    # Parent dirs are created so first ATTACH succeeds on a fresh checkout.
    assert catalog.parent.is_dir()
    assert data_dir.is_dir()


def test_adapter_schema_option_sets_namespace(monkeypatch: pytest.MonkeyPatch, tmp_path):
    log: dict[str, Any] = {}
    _install_fake_duckdb(monkeypatch, log)
    adapter = DuckLakeAdapter(
        {"catalog_path": str(tmp_path / "cat.ducklake"), "schema": "analytics"}
    )

    adapter.query("select 1")

    assert log["setup_sql"][-1] == 'USE jaffle."analytics"'
    assert log["cursor_sql"][0] == 'USE jaffle."analytics"'


def test_adapter_resolves_relative_paths_against_repo_root(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = DuckLakeAdapter(
        {
            "catalog_path": "data/.ducklake/catalog.ducklake",
            "data_path": "data/.ducklake/files",
        }
    )

    catalog_path, data_path = adapter._resolve_paths()

    assert os.path.isabs(catalog_path) and os.path.isabs(data_path)
    assert catalog_path == os.path.join(repo_root(), "data/.ducklake/catalog.ducklake")
    assert data_path == os.path.join(repo_root(), "data/.ducklake/files")


def test_adapter_redacts_query_errors(monkeypatch: pytest.MonkeyPatch, tmp_path):
    log: dict[str, Any] = {"fail_query": True}
    _install_fake_duckdb(monkeypatch, log)
    adapter = DuckLakeAdapter({"catalog_path": str(tmp_path / "cat.ducklake")})

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("SELECT secret_column FROM jaffle_order")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "ducklake"
    assert details["connection_kind"] == "ducklake_native"
    assert details["option_keys"] == ["catalog_path"]
    assert details["sql_redacted"] is True
    # Raw SQL and option VALUES never leak into details.
    assert "sql" not in details
    assert str(tmp_path) not in repr(details)
    assert "secret_column" not in repr(details)


def test_create_adapter_entry_point_and_factory(monkeypatch: pytest.MonkeyPatch, tmp_path):
    package = PackageMeta(
        package_id="lake_demo",
        name="lake_demo",
        description="ducklake demo",
        warehouse="ducklake",
        connection=ConnectionSpec(
            kind="ducklake_native",
            options={"catalog_path": str(tmp_path / "cat.ducklake")},
        ),
    )

    adapter = create_warehouse_adapter(package)
    assert isinstance(adapter, DuckLakeAdapter)
    assert adapter.options == {"catalog_path": str(tmp_path / "cat.ducklake")}

    with pytest.raises(SemanticLayerError) as exc:
        create_adapter(replace(package, connection=ConnectionSpec(kind="duckdb_native")))
    assert exc.value.code == "INVALID_CONFIG"
    assert "Unsupported DuckLake connection kind" in str(exc.value)


# ---------------------------------------------------------------------------
# Compile-only sweep — the full conformance battery compiles for ducklake.
# ---------------------------------------------------------------------------


def _ducklake_config():
    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="ducklake",
            default_db="",
            seed=replace(config.package.seed, kind="", source="", post_sql=""),
            connection=ConnectionSpec(
                kind="ducklake_native",
                options={
                    "catalog_path_env": "SR_DUCKLAKE_CATALOG_PATH",
                    "data_path_env": "SR_DUCKLAKE_DATA_PATH",
                },
            ),
        ),
    )


def test_ducklake_compiles_entire_conformance_battery():
    from tests.integration.harness import load_battery

    battery = load_battery()
    assert len(battery) >= 15, f"battery shrank to {len(battery)} cases"

    config = _ducklake_config()
    registry = Registry(config)
    duckdb_config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    duckdb_registry = Registry(duckdb_config)

    for case in battery:
        compiled = compile_query(config, registry, case.payload)
        sql = compiled["sql"]
        assert sql and sql.strip(), f"{case.name}: empty SQL for ducklake"
        # DuckLake is DuckDB SQL: the compiled text must be identical to
        # the DuckDB reference compilation for every battery case.
        reference_sql = compile_query(duckdb_config, duckdb_registry, case.payload)["sql"]
        assert sql == reference_sql, f"{case.name}: ducklake SQL diverged from duckdb"
