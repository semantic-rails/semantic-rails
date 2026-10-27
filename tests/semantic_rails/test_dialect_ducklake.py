"""Offline tests for the DuckLake warehouse connector.

Three layers, no network and no live warehouse:

- dialect: DuckLake IS DuckDB SQL — the dialect subclasses DuckDbDialect
  with zero overrides (the DuckDB forms are pinned in the MotherDuck tests);
- adapter: option normalization, secret/unknown-option rejection,
  missing-env errors, redacted error envelopes, and the exact
  INSTALL/LOAD/ATTACH/USE bootstrap (driver faked via sys.modules);
- compile-only sweep: the full conformance battery compiles for
  warehouse=ducklake without any connection.
"""

from __future__ import annotations

import os
import types
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, repo_root, resolve_repo_path
from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts import duckdb_setup
from semantic_rails.db_parts.ducklake import DuckLakeAdapter, create_adapter
from semantic_rails.dialects import (
    DuckDbDialect,
    DuckLakeDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.schema import ConnectionSpec, PackageMeta


def test_ducklake_dialect_is_duckdb_subclass_with_no_overrides():
    dialect = dialect_for_warehouse("ducklake")
    assert isinstance(dialect, DuckLakeDialect) and isinstance(dialect, DuckDbDialect)
    assert dialect.name == "ducklake"
    # Contract: DuckLake is DuckDB SQL over an attached catalog — the
    # dialect class must not override any rendering hook.
    overridden = set(DuckLakeDialect.__dict__) & {
        name for name in dir(DuckDbDialect) if not name.startswith("__")
    }
    assert overridden - {"name"} == set()

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

    def sql(self, sql: str, *, params):
        assert params == []
        self.execute(sql)
        return SimpleNamespace(description=self.description, fetchall=lambda: [(1, "x")])

    def close(self) -> None:
        self._log["cursor_closed"] = True


class FakeConnection:
    def __init__(self, log: dict[str, Any]):
        self._log = log

    def execute(self, sql: str, parameters=None):
        self._log.setdefault("setup_sql", []).append(sql)
        return self

    def fetchone(self):
        return ("",)

    def fetchall(self):
        return []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._log)

    def close(self) -> None:
        self._log["connection_closed"] = True


def _install_fake_duckdb(monkeypatch: pytest.MonkeyPatch, log: dict[str, Any]) -> None:
    module = types.ModuleType("duckdb")
    module.connect = lambda *args, **kwargs: FakeConnection(log)
    monkeypatch.setattr(duckdb_setup, "duckdb", module)


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


def test_adapter_reports_missing_env_and_catalog_without_connecting(
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

    with pytest.raises(SemanticLayerError) as exc:
        DuckLakeAdapter({"schema": "main"}).query("select 1")
    assert exc.value.code == "INVALID_CONFIG"
    assert "catalog_path" in str(exc.value)
    assert "setup_sql" not in log  # never connected


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
        "SELECT database_name, function_name, function_type FROM system.main.duckdb_functions()",
        "SELECT system.main.current_setting('disabled_optimizers')",
        "SET disabled_optimizers = ?",
        "SELECT system.main.current_setting('lock_configuration')",
        "SET enable_progress_bar = false",
        "INSTALL ducklake",
        "LOAD ducklake",
        f"ATTACH IF NOT EXISTS 'ducklake:{catalog}' AS jaffle (DATA_PATH '{data_dir}')",
        "SELECT database_name, function_name, function_type FROM system.main.duckdb_functions()",
        "SELECT system.main.current_setting('disabled_optimizers')",
        "SET disabled_optimizers = ?",
        "SELECT system.main.current_setting('lock_configuration')",
        "SET enable_progress_bar = false",
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
    assert exc.value.details["connection_kind"] == "duckdb_native"


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
        query, _ = read_call(case.payload, config=config)
        # Coverage SQL caps at the request's now: pin it so both compilations read one instant.
        query = {**query, "policy_context": {"now": "2026-10-09T00:00:00Z"}}
        compiled = compile_query(config, registry, query)
        sql = compiled["sql"]
        assert sql and sql.strip(), f"{case.name}: empty SQL for ducklake"
        # DuckLake is DuckDB SQL: the compiled text must be identical to
        # the DuckDB reference compilation for every battery case.
        reference_sql = compile_query(duckdb_config, duckdb_registry, query)["sql"]
        assert sql == reference_sql, f"{case.name}: ducklake SQL diverged from duckdb"
