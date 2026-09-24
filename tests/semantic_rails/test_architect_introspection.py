"""Read-only warehouse introspection: list, describe, profile, suggest."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import duckdb
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_introspection import (
    MAX_PROFILE_ROWS,
    MAX_SAMPLE_CHARS,
    MAX_SAMPLE_VALUES,
    describe_table,
    list_tables,
    open_duckdb,
    profile_columns,
    suggest_model,
)
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import (
    build_dbt_warehouse,
    file_digest,
    write_orders_package,
)


@pytest.fixture()
def warehouse_path(tmp_path: Path) -> Path:
    return build_dbt_warehouse(tmp_path / "warehouse.duckdb")


def _by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(row[key]): row for row in rows}


def test_list_tables_shows_tables_and_views_by_schema(warehouse_path: Path) -> None:
    with open_duckdb(warehouse_path) as warehouse:
        everything = _by(list_tables(warehouse), "relation")
        marts = list_tables(warehouse, schema="main_marts")

    assert everything["main_marts.fct_orders"]["kind"] == "table"
    assert everything["main_staging.stg_orders"]["kind"] == "view"
    assert everything["raw_orders"]["schema"] == "main"
    assert {row["relation"] for row in marts} == {
        "main_marts.dim_customers",
        "main_marts.dim_products",
        "main_marts.dim_stores",
        "main_marts.fct_order_lines",
        "main_marts.fct_orders",
    }


def test_describe_table_reports_types_nullability_and_declared_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "keys.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE, tier VARCHAR "
        "DEFAULT 'free');"
        "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL "
        "REFERENCES customers (id), total DECIMAL(10, 2));"
    )
    conn.close()

    with open_duckdb(db_path) as warehouse:
        customers = describe_table(warehouse, "customers")
        orders = describe_table(warehouse, "main.orders")

    assert customers["primary_key"] == ["id"]
    assert customers["unique"] == [["email"]]
    assert _by(customers["columns"], "name")["tier"]["default"] == "'free'"
    columns = _by(orders["columns"], "name")
    assert columns["customer_id"]["nullable"] is False
    assert columns["total"]["type"] == "DECIMAL(10,2)"
    assert columns["total"]["nullable"] is True
    assert orders["foreign_keys"] == [
        {"columns": ["customer_id"], "references": {"relation": "customers", "columns": ["id"]}}
    ]


def test_profile_counts_and_caps_what_it_returns(tmp_path: Path) -> None:
    db_path = tmp_path / "wide.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE events AS SELECT range AS id, 'x' || (range % 30) AS kind, "
        "CASE WHEN range % 4 = 0 THEN NULL ELSE range END AS maybe, "
        "repeat('y', 500) || range AS note FROM range(1000)"
    )
    conn.close()

    with open_duckdb(db_path) as warehouse:
        full = profile_columns(warehouse, "events", sample_limit=99)
        sampled = profile_columns(warehouse, "events", ["kind"], sample_limit=0, max_rows=100)

    columns = _by(full["columns"], "name")
    assert full["row_count"] == 1000 and full["sampled"] is False
    assert columns["id"]["distinct_count"] == 1000 and (
        columns["id"]["min"],
        columns["id"]["max"],
    ) == (0, 999)
    assert columns["maybe"]["null_count"] == 250
    assert columns["kind"]["distinct_count"] == 30
    assert len(columns["kind"]["samples"]) == MAX_SAMPLE_VALUES
    assert all(len(value) <= MAX_SAMPLE_CHARS for value in columns["note"]["samples"])
    assert sampled["sampled"] is True and sampled["rows_profiled"] == 100
    assert sampled["columns"][0]["samples"] == []


@pytest.mark.parametrize(
    ("relation", "error"),
    [("missing_table", "OBJECT_NOT_FOUND"), ("main_marts.fct_orders; DROP", "INVALID_QUERY")],
)
def test_unknown_or_unsafe_relations_are_refused(
    warehouse_path: Path, relation: str, error: str
) -> None:
    with open_duckdb(warehouse_path) as warehouse, pytest.raises(SemanticLayerError) as excinfo:
        describe_table(warehouse, relation)
    assert excinfo.value.code == error


def test_introspection_quotes_project_relation_components(tmp_path: Path) -> None:
    db_path = tmp_path / "quoted.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute('CREATE SCHEMA "sales-data"')
    conn.execute(
        'CREATE TABLE "sales-data"."fct""orders" '
        '("order id" INTEGER PRIMARY KEY, "net amount" DECIMAL(10, 2))'
    )
    conn.execute('INSERT INTO "sales-data"."fct""orders" VALUES (1, 12.5)')
    conn.close()

    relation = 'sales-data.fct"orders'
    with open_duckdb(db_path) as warehouse:
        described = describe_table(warehouse, relation)
        profiled = profile_columns(warehouse, relation, ["net amount"])
        suggested = suggest_model(warehouse, relation)

    assert described["primary_key"] == ["order id"]
    assert profiled["row_count"] == 1
    assert profiled["columns"][0]["samples"] == ["12.50"]
    assert suggested["relation"] == relation
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp_described,) = _session(
        server, [("describe_table", {"relation": relation, "duckdb_path": str(db_path)})]
    )
    assert mcp_described["ok"] is True and mcp_described["primary_key"] == ["order id"]


def test_profile_row_cap_cannot_be_raised_by_the_caller(tmp_path: Path) -> None:
    db_path = tmp_path / "large.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(f"CREATE TABLE events AS SELECT range AS id FROM range({MAX_PROFILE_ROWS + 7})")
    conn.close()

    with open_duckdb(db_path) as warehouse:
        profile = profile_columns(
            warehouse, "events", ["id"], sample_limit=0, max_rows=MAX_PROFILE_ROWS * 10
        )

    assert profile["row_count"] == MAX_PROFILE_ROWS + 7
    assert profile["rows_profiled"] == MAX_PROFILE_ROWS
    assert profile["sampled"] is True
    assert profile["columns"][0]["distinct_count"] <= MAX_PROFILE_ROWS


def test_introspection_never_writes_or_creates_a_database(
    warehouse_path: Path, tmp_path: Path
) -> None:
    before = file_digest(warehouse_path)
    with open_duckdb(warehouse_path) as warehouse:
        list_tables(warehouse)
        profile_columns(warehouse, "main_marts.fct_orders")
        suggest_model(warehouse, "main_marts.fct_order_lines")

    assert file_digest(warehouse_path) == before
    missing = tmp_path / "nothing_here.duckdb"
    with pytest.raises(SemanticLayerError, match="does not exist"), open_duckdb(missing):
        pass
    assert not missing.exists()


def test_suggest_model_for_a_fact_table(warehouse_path: Path) -> None:
    with open_duckdb(warehouse_path) as warehouse:
        orders = suggest_model(warehouse, "main_marts.fct_orders")

    assert orders["entity"] == "order"
    assert orders["primary_key"]["columns"] == ["order_id"]
    assert orders["primary_key"]["confidence"] == "high"
    times = _by(orders["times"], "column")
    assert times["ordered_at"]["confidence"] == "high"
    assert times["delivered_at"]["confidence"] == "medium"  # it has nulls
    assert _by(orders["dimensions"], "column")["status"]["confidence"] == "high"
    measures = _by(orders["measures"], "key")
    assert measures["order_total"]["aggregation"] == "sum"
    links = _by(orders["foreign_keys"], "column")
    assert links["customer_id"]["references"]["relation"] == "main_marts.dim_customers"
    assert links["customer_id"]["confidence"] == "high"  # a declared key, no orphans
    assert links["store_id"]["confidence"] == "medium"  # unique there, not declared


def test_suggest_model_for_a_line_table_finds_the_composite_key(warehouse_path: Path) -> None:
    with open_duckdb(warehouse_path) as warehouse:
        lines = suggest_model(warehouse, "main_marts.fct_order_lines")

    assert lines["primary_key"]["columns"] == ["order_id", "line_number"]
    links = _by(lines["foreign_keys"], "column")
    assert links["order_id"]["references"]["relation"] == "main_marts.fct_orders"
    assert links["product_id"]["references"]["relation"] == "main_marts.dim_products"
    measures = _by(lines["measures"], "key")
    assert measures["unit_price"]["aggregation"] == "avg"  # summing unit prices is wrong
    assert measures["net_amount"]["aggregation"] == "sum"


def test_a_drafted_model_validates_against_the_warehouse(tmp_path: Path) -> None:
    """suggest_model's draft feeds upsert_model; the result parses and runs."""
    project = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(project / "data" / "warehouse.duckdb")
    with open_duckdb(project / "data" / "warehouse.duckdb") as warehouse:
        draft = suggest_model(warehouse, "main_marts.dim_customers")["upsert_model"]

    mutation = ArchitectProject(project, workspace_root=tmp_path).upsert_model(**draft)
    assert mutation.report["ok"] is True, mutation.report

    runtime = Runtime.from_path(str(project))
    try:
        rows = runtime.query(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.shop.customer_count"}, "as": "customers"}
                ],
                "limit": 5,
            }
        )["rows"]
    finally:
        runtime.close()
    assert rows == [{"customers": 5}]


def _session(server: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        results = []
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            for name in ("list_tables", "describe_table", "profile_columns", "suggest_model"):
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.readOnlyHint is True and annotations.destructiveHint is False
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                results.append(dict(result.structuredContent or {}))
        return results

    return asyncio.run(run())


def test_mcp_session_explores_a_dbt_warehouse(tmp_path: Path) -> None:
    build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    path = {"duckdb_path": "dbt/warehouse.duckdb"}

    tables, described, profiled, suggested = _session(
        server,
        [
            ("list_tables", {**path, "schema": "main_marts"}),
            ("describe_table", {**path, "relation": "main_marts.dim_customers"}),
            ("profile_columns", {**path, "relation": "main_marts.fct_orders", "sample_limit": 2}),
            ("suggest_model", {**path, "relation": "main_marts.fct_orders"}),
        ],
    )

    assert tables["ok"] is True and len(tables["tables"]) == 5
    assert described["primary_key"] == ["customer_id"]
    assert profiled["row_count"] == 8
    assert suggested["upsert_model"]["primary_key"] == ["order_id"]


def test_mcp_tools_stay_inside_the_workspace(tmp_path: Path) -> None:
    outside = build_dbt_warehouse(tmp_path / "outside.duckdb")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = create_architect_mcp_server(workspace_root=workspace)

    escaped, both = _session(
        server,
        [
            ("list_tables", {"duckdb_path": str(outside)}),
            ("list_tables", {"duckdb_path": "x.duckdb", "project_path": "x"}),
        ],
    )

    assert escaped["ok"] is False and "workspace root" in escaped["error"]["message"]
    assert both["ok"] is False and both["error"]["code"] == "INVALID_MCP_ARGUMENTS"
