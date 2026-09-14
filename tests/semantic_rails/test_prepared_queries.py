"""Compilation is authoritative at the driver boundary; no live warehouses used."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from semantic_rails.cache import CachedCompilation, LruCompiledSqlCache
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.db import SnowflakeCliAdapter, SnowflakeNativeAdapter, WarehouseAdapter
from semantic_rails.db_parts.athena import AthenaAdapter
from semantic_rails.db_parts.bigquery import BigQueryNativeAdapter
from semantic_rails.db_parts.databricks import DatabricksNativeAdapter
from semantic_rails.db_parts.postgres import PostgresAdapter
from semantic_rails.registry import Registry
from semantic_rails.schema import ConnectionSpec, SeedSpec
from semantic_rails.sql_preparation import PreparedQuery, prepare_query

_ALIAS = "average_order_value_" * 5


@pytest.fixture(scope="module")
def package_config():
    return load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))


def _compile(package_config, warehouse, profile):
    config = replace(
        package_config,
        package=replace(
            package_config.package,
            warehouse=warehouse,
            default_db="",
            seed=SeedSpec(),
            connection=ConnectionSpec(kind=f"{warehouse}_native"),
        ),
    )
    return compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "select": [{"expression": {"metric": "metric.sales.aov_usd"}, "as": _ALIAS}],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "start": "2016-09-01",
                "end": "2016-10-01",
            },
            "sql_profile": profile,
        },
    )


def _physical_alias(prepared):
    return next((key for key, value in prepared.column_mapping if value == _ALIAS), _ALIAS)


class CaptureCursor:
    def __init__(self, column):
        self.description = [(column,)]
        self.statements = []
        self.closed = False
        self.fetch_size = None

    def execute(self, sql):
        self.statements.append(sql)

    def fetchmany(self, size):
        self.fetch_size = size
        return [(1.25,), (2.5,)]

    def close(self):
        self.closed = True


def _forbid_second_preparation(*args, **kwargs):
    raise AssertionError("compiled SQL must reach the driver without a second preparation")


@pytest.mark.parametrize("profile", ["audit", "compact", "debug", "off"])
@pytest.mark.parametrize("warehouse", ["postgres", "databricks", "athena", "snowflake"])
def test_compiled_sql_is_the_dbapi_statement(package_config, monkeypatch, warehouse, profile):
    compiled = _compile(package_config, warehouse, profile)
    prepared = compiled["prepared_query"]
    assert compiled["sql"] == compiled["explain"].rendered_sql == prepared.sql
    assert "/ CAST(NULLIF(" in prepared.sql
    if warehouse == "postgres":
        assert _ALIAS in dict(prepared.column_mapping).values()
        assert "AS DOUBLE PRECISION)" in prepared.sql
    if warehouse == "athena":
        assert "TIMESTAMP '2016-09-01'" in prepared.sql
    if warehouse == "databricks":
        assert "`dimension.jaffle_store_name`" in prepared.sql

    adapter = {
        "postgres": PostgresAdapter,
        "databricks": DatabricksNativeAdapter,
        "athena": AthenaAdapter,
        "snowflake": lambda: SnowflakeNativeAdapter("test_connection"),
    }[warehouse]()
    cursor = CaptureCursor(_physical_alias(prepared))
    adapter._conn = SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr("semantic_rails.db_parts.common.prepare_query", _forbid_second_preparation)
    monkeypatch.setattr(
        "semantic_rails.db_parts.snowflake.prepare_query", _forbid_second_preparation
    )
    rows = adapter.query_prepared(prepared, limits={"max_rows": 1, "statement_timeout_ms": 1000})

    expected = {
        "postgres": ["SET statement_timeout = 1000", prepared.sql, "RESET statement_timeout"],
        "databricks": ["SET STATEMENT_TIMEOUT = 1", prepared.sql, "RESET STATEMENT_TIMEOUT"],
        "athena": [prepared.sql],
        "snowflake": [
            "alter session set statement_timeout_in_seconds = 1",
            prepared.sql,
            "alter session unset statement_timeout_in_seconds",
        ],
    }[warehouse]
    assert cursor.statements == expected
    assert rows == [{_ALIAS: 1.25}]
    assert rows.truncated is True
    assert cursor.fetch_size == 2
    assert cursor.closed


@pytest.mark.parametrize("profile", ["audit", "compact", "debug", "off"])
def test_compiled_bigquery_sql_and_original_columns_survive_execution(
    package_config, monkeypatch, profile
):
    compiled = _compile(package_config, "bigquery", profile)
    prepared = compiled["prepared_query"]
    assert compiled["sql"] == compiled["explain"].rendered_sql == prepared.sql
    names = dict(prepared.column_mapping)
    physical_dimension = next(
        key for key, value in names.items() if value == "dimension.jaffle_store_name"
    )
    assert f"`{physical_dimension}`" in prepared.sql
    statements = []

    def query(sql, *, job_config):
        statements.append(sql)
        assert job_config.job_timeout_ms == 1000
        return SimpleNamespace(
            result=lambda: [{physical_dimension: "Portland"}, {physical_dimension: "NYC"}]
        )

    adapter = BigQueryNativeAdapter()
    adapter._client = SimpleNamespace(query=query)
    monkeypatch.setattr(
        adapter, "_bigquery", lambda: SimpleNamespace(QueryJobConfig=SimpleNamespace)
    )
    monkeypatch.setattr(
        "semantic_rails.db_parts.bigquery.prepare_query", _forbid_second_preparation
    )
    rows = adapter.query_prepared(prepared, limits={"max_rows": 1, "statement_timeout_ms": 1000})
    assert statements == [compiled["sql"]]
    assert rows == [{"dimension.jaffle_store_name": "Portland"}]
    assert rows.truncated is True


@pytest.mark.parametrize("timeout_ms", [0, 1000])
def test_compiled_snowflake_cli_statement_only_adds_session_controls(
    package_config, monkeypatch, timeout_ms
):
    prepared = _compile(package_config, "snowflake", "audit")["prepared_query"]
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("semantic_rails.db_parts.snowflake.subprocess.run", run)
    monkeypatch.setattr(
        "semantic_rails.db_parts.snowflake.prepare_query", _forbid_second_preparation
    )
    SnowflakeCliAdapter("test_connection").query_prepared(
        prepared, limits={"statement_timeout_ms": timeout_ms}
    )
    expected = (
        prepared.sql
        if not timeout_ms
        else (
            "alter session set statement_timeout_in_seconds = 1;\n"
            f"{prepared.sql};\n"
            "alter session unset statement_timeout_in_seconds;"
        )
    )
    assert commands[0][-1] == expected


def test_postgres_direct_query_preserves_long_unicode_aliases():
    aliases = ["値" * 30 + suffix for suffix in ("_a", "_b")]
    original = f'SELECT 1 AS "{aliases[0]}", 2 AS "{aliases[1]}"'
    prepared = prepare_query(original, "postgres")
    names = dict(prepared.column_mapping)
    assert set(names.values()) == set(aliases)
    assert len(names) == 2
    assert all(len(name.encode("utf-8")) <= 63 for name in names)
    cursor = CaptureCursor(next(iter(names)))
    adapter = PostgresAdapter()
    adapter._conn = SimpleNamespace(cursor=lambda: cursor)
    rows = adapter.query(original, limits={"max_rows": 1})
    assert cursor.statements == [prepared.sql]
    assert rows == [{aliases[0]: 1.25}]
    assert rows.truncated is True


def test_custom_adapter_query_compatibility():
    class CustomAdapter(WarehouseAdapter):
        engine = "custom"

        def query(self, sql, *, limits=None):
            assert sql == "SELECT 1"
            assert limits == {"max_rows": 1}
            return [{"physical": 1}]

        def close(self):
            pass

    prepared = PreparedQuery("SELECT 1", (("physical", "semantic"),))
    assert CustomAdapter().query_prepared(prepared, limits={"max_rows": 1}) == [{"semantic": 1}]


def test_compiled_cache_preserves_prepared_artifact_and_isolation(package_config):
    compiled = _compile(package_config, "bigquery", "audit")
    expected = compiled["prepared_query"]
    cache = LruCompiledSqlCache()
    cache.put("query", CachedCompilation(compiled))
    # Cache values are typed, process-local objects, not a JSON wire format.
    compiled["prepared_query"] = PreparedQuery("changed after put")
    cached = cache.get("query")
    assert cached is not None
    assert isinstance(cached.compiled["prepared_query"], PreparedQuery)
    assert cached.compiled["prepared_query"] == expected
    cached.compiled["prepared_query"] = PreparedQuery("changed after get")
    assert cache.get("query").compiled["prepared_query"] == expected


def test_custom_adapter_without_limits_keeps_legacy_signature():
    class LegacyAdapter(WarehouseAdapter):
        engine = "custom"

        def query(self, sql):
            assert sql == "SELECT 1"
            return [{"physical": 1}]

        def close(self):
            pass

    prepared = PreparedQuery("SELECT 1", (("physical", "semantic"),))
    assert LegacyAdapter().query_prepared(prepared, limits={"max_rows": 1}) == [{"semantic": 1}]
