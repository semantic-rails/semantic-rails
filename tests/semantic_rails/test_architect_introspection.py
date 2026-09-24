"""Read-only warehouse introspection: list, describe, profile, suggest."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_introspection import (
    MAX_PROFILE_ROWS,
    MAX_SAMPLE_CHARS,
    MAX_SAMPLE_VALUES,
    _relation_name,
    _split_relation,
    classify_column,
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


def test_declared_foreign_key_keeps_schema_and_quoted_composite_target(tmp_path: Path) -> None:
    db_path = tmp_path / "keys.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA main_marts")
        conn.execute("CREATE TABLE main.customers (customer_id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE main_marts.customers (customer_id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE main_marts.orders (order_id INTEGER PRIMARY KEY, "
            "customer_id INTEGER REFERENCES main_marts.customers(customer_id))"
        )
        conn.execute('CREATE SCHEMA "sales-data"')
        conn.execute(
            'CREATE TABLE "sales-data"."dim""customers" '
            '("tenant id" INTEGER, "customer-id" INTEGER, '
            'PRIMARY KEY ("tenant id", "customer-id"))'
        )
        conn.execute(
            'CREATE TABLE "sales-data"."fct orders" '
            '("tenant_id" INTEGER, "customer_id" INTEGER, '
            'FOREIGN KEY ("tenant_id", "customer_id") REFERENCES '
            '"sales-data"."dim""customers" ("tenant id", "customer-id"))'
        )

    with open_duckdb(db_path) as warehouse:
        orders = describe_table(warehouse, "main_marts.orders")
        suggested = suggest_model(warehouse, "main_marts.orders")
        target = describe_table(warehouse, orders["foreign_keys"][0]["references"]["relation"])
        quoted = describe_table(warehouse, "sales-data.fct orders")
        quoted_suggested = suggest_model(warehouse, "sales-data.fct orders")
        quoted_target = describe_table(
            warehouse, quoted["foreign_keys"][0]["references"]["relation"]
        )

    assert orders["foreign_keys"] == [
        {
            "columns": ["customer_id"],
            "references": {"relation": "main_marts.customers", "columns": ["customer_id"]},
        }
    ]
    assert target["relation"] == "main_marts.customers"
    assert suggested["foreign_keys"] == [
        {
            "column": "customer_id",
            "references": orders["foreign_keys"][0]["references"],
            "confidence": "high",
            "reason": "declared FOREIGN KEY",
        }
    ]
    assert quoted["foreign_keys"] == [
        {
            "columns": ["tenant_id", "customer_id"],
            "references": {
                "relation": '"sales-data"."dim""customers"',
                "columns": ["tenant id", "customer-id"],
            },
        }
    ]
    assert quoted_target["primary_key"] == ["tenant id", "customer-id"]
    assert quoted_suggested["foreign_keys"] == [
        {
            "columns": ["tenant_id", "customer_id"],
            "references": quoted["foreign_keys"][0]["references"],
            "confidence": "high",
            "reason": "declared FOREIGN KEY",
        }
    ]
    assert not {"tenant_id", "customer_id"} & {
        item["column"] for item in quoted_suggested["dimensions"]
    }
    assert not {"tenant_id", "customer_id"} & set(quoted_suggested["upsert_model"]["dimensions"])

    server = create_architect_mcp_server(workspace_root=tmp_path)
    path = {"duckdb_path": "keys.duckdb"}
    mcp_orders, mcp_suggested, mcp_target, mcp_quoted, mcp_quoted_suggested, mcp_quoted_target = (
        _session(
            server,
            [
                ("describe_table", {**path, "relation": "main_marts.orders"}),
                ("suggest_model", {**path, "relation": "main_marts.orders"}),
                ("describe_table", {**path, "relation": "main_marts.customers"}),
                ("describe_table", {**path, "relation": "sales-data.fct orders"}),
                ("suggest_model", {**path, "relation": "sales-data.fct orders"}),
                ("describe_table", {**path, "relation": 'sales-data.dim"customers'}),
            ],
        )
    )
    assert mcp_orders["foreign_keys"] == orders["foreign_keys"]
    assert mcp_suggested["foreign_keys"][0]["references"] == orders["foreign_keys"][0]["references"]
    assert mcp_target["relation"] == mcp_orders["foreign_keys"][0]["references"]["relation"]
    assert mcp_quoted["foreign_keys"] == quoted["foreign_keys"]
    assert mcp_quoted_suggested["foreign_keys"] == quoted_suggested["foreign_keys"]
    assert mcp_quoted_target["relation"] == mcp_quoted["foreign_keys"][0]["references"]["relation"]


@pytest.mark.parametrize("parent_order", [("main", "main_marts"), ("main_marts", "main")])
def test_undeclared_foreign_key_reports_all_matching_targets(
    tmp_path: Path, parent_order: tuple[str, str]
) -> None:
    db_path = tmp_path / "ambiguous.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA main_marts")
        for schema in parent_order:
            conn.execute(f"CREATE TABLE {schema}.customers (customer_id INTEGER PRIMARY KEY)")
            conn.execute(f"INSERT INTO {schema}.customers VALUES (1)")
        conn.execute(
            "CREATE TABLE main_marts.orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER)"
        )
        conn.execute("INSERT INTO main_marts.orders VALUES (10, 1)")

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "main_marts.orders")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp_suggested,) = _session(
        server,
        [("suggest_model", {"duckdb_path": db_path.name, "relation": "main_marts.orders"})],
    )
    links = suggested["foreign_keys"]
    assert {link["references"]["relation"] for link in links} == {
        "customers",
        "main_marts.customers",
    }
    assert all(link["column"] == "customer_id" for link in links)
    assert all(link["confidence"] == "low" for link in links)
    assert all("multiple possible target relations" in link["reason"] for link in links)
    assert mcp_suggested["foreign_keys"] == links


def test_undeclared_foreign_key_with_one_matching_target_remains_high_confidence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "unambiguous.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA main_marts")
        conn.execute("CREATE TABLE main.customers (customer_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO main.customers VALUES (2)")
        conn.execute("CREATE TABLE main_marts.customers (customer_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO main_marts.customers VALUES (1)")
        conn.execute(
            "CREATE TABLE main_marts.orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER)"
        )
        conn.execute("INSERT INTO main_marts.orders VALUES (10, 1)")

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "main_marts.orders")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp_suggested,) = _session(
        server,
        [("suggest_model", {"duckdb_path": db_path.name, "relation": "main_marts.orders"})],
    )
    assert suggested["foreign_keys"] == [
        {
            "column": "customer_id",
            "references": {"relation": "main_marts.customers", "columns": ["customer_id"]},
            "confidence": "high",
            "reason": "main_marts.customers.customer_id is its declared key; every value here matches a row there",
        }
    ]
    assert mcp_suggested["foreign_keys"] == suggested["foreign_keys"]


def test_undeclared_foreign_key_target_cap_does_not_imply_unique_target(tmp_path: Path) -> None:
    db_path = tmp_path / "many-targets.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER)")
        conn.execute("INSERT INTO orders VALUES (10, 1)")
        for index in range(9):
            schema = f"s{index:02d}"
            conn.execute(f"CREATE SCHEMA {schema}")
            conn.execute(f"CREATE TABLE {schema}.customers (customer_id INTEGER PRIMARY KEY)")
            conn.execute(f"INSERT INTO {schema}.customers VALUES ({1 if index == 0 else 2})")

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "orders")
    links = suggested["foreign_keys"]
    assert len(links) == 1
    assert links[0]["references"]["relation"] == "s00.customers"
    assert links[0]["confidence"] == "low"
    assert "additional target relations were not checked" in links[0]["reason"]


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
    [
        ("missing_table", "OBJECT_NOT_FOUND"),
        ("main_marts.fct_orders; DROP", "INVALID_QUERY"),
        ('"unterminated', "INVALID_QUERY"),
        ('main."unterminated', "INVALID_QUERY"),
        ('main.""', "INVALID_QUERY"),
        ('"valid"garbage', "INVALID_QUERY"),
        ('main."valid"garbage', "INVALID_QUERY"),
        ("main.orders.extra", "INVALID_QUERY"),
        ("main..orders", "INVALID_QUERY"),
        ("main.orders.", "INVALID_QUERY"),
    ],
)
def test_unknown_or_unsafe_relations_are_refused(
    warehouse_path: Path, relation: str, error: str
) -> None:
    with open_duckdb(warehouse_path) as warehouse, pytest.raises(SemanticLayerError) as excinfo:
        describe_table(warehouse, relation)
    assert excinfo.value.code == error


RELATION_IDENTITY_CASES = [
    ("main", "orders"),
    ("sales", "orders"),
    ("main", "sales.orders"),
    ("sales.v1", "orders.2026"),
    ("select", "from"),
    ("sales data", 'order"lines'),
    ("données", "注文"),
    ("main", 'say "hello"'),
    ("main", '"leading quote'),
    ('sales"data', '"quote"inside'),
]


@pytest.mark.parametrize(("schema", "name"), RELATION_IDENTITY_CASES)
def test_relation_identity_round_trips_distinct_components(schema: str, name: str) -> None:
    assert _split_relation(_relation_name(schema, name)) == (schema, name)


def test_relation_identity_distinguishes_colliding_raw_dots() -> None:
    assert len({_relation_name(*pair) for pair in RELATION_IDENTITY_CASES}) == len(
        RELATION_IDENTITY_CASES
    )
    assert _split_relation("main.orders") == ("main", "orders")
    assert _split_relation('main."sales.orders"') == ("main", "sales.orders")
    assert _split_relation('"sales.v1"."orders.2026"') == ("sales.v1", "orders.2026")


def test_dotted_relation_identity_round_trips_service_and_mcp(tmp_path: Path) -> None:
    db_path = tmp_path / "dotted.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA sales")
        conn.execute('CREATE SCHEMA "sales.v1"')
        conn.execute('CREATE TABLE main."sales.orders" (id INTEGER PRIMARY KEY, amount INTEGER)')
        conn.execute('INSERT INTO main."sales.orders" VALUES (11, 110)')
        conn.execute("CREATE TABLE sales.orders (id INTEGER PRIMARY KEY, note VARCHAR)")
        conn.execute("INSERT INTO sales.orders VALUES (22, 'different relation')")
        conn.execute('CREATE TABLE "sales.v1"."customers.2026" (customer_id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO "sales.v1"."customers.2026" VALUES (1)')
        conn.execute(
            'CREATE TABLE "sales.v1"."orders.2026" '
            "(order_id INTEGER PRIMARY KEY, customer_id INTEGER "
            'REFERENCES "sales.v1"."customers.2026"(customer_id))'
        )
        conn.execute('INSERT INTO "sales.v1"."orders.2026" VALUES (33, 1)')
        conn.execute(
            'CREATE TABLE "sales.v1"."shipments.2026" '
            "(shipment_id INTEGER PRIMARY KEY, customer_id INTEGER)"
        )
        conn.execute('INSERT INTO "sales.v1"."shipments.2026" VALUES (44, 1)')

    expected = {
        '"sales.orders"': ("main", "sales.orders", 11),
        "sales.orders": ("sales", "orders", 22),
        '"sales.v1"."orders.2026"': ("sales.v1", "orders.2026", 33),
        '"sales.v1"."customers.2026"': ("sales.v1", "customers.2026", 1),
        '"sales.v1"."shipments.2026"': ("sales.v1", "shipments.2026", 44),
    }
    with open_duckdb(db_path) as warehouse:
        listed = _by(list_tables(warehouse), "relation")
        assert set(listed) == set(expected)
        assert {row["relation"] for row in list_tables(warehouse, schema="sales.v1")} == {
            relation for relation, (schema, _, _) in expected.items() if schema == "sales.v1"
        }
        for relation, (schema, name, value) in expected.items():
            assert (listed[relation]["schema"], listed[relation]["name"]) == (schema, name)
            assert describe_table(warehouse, relation)["relation"] == relation
            profile = profile_columns(warehouse, relation, sample_limit=1)
            assert profile["row_count"] == 1
            assert profile["columns"][0]["samples"] == [value]
            suggestion = suggest_model(warehouse, relation)
            assert suggestion["relation"] == relation
            assert suggestion["upsert_model"]["relation"] == relation
            if "." in schema or "." in name:
                assert "cannot execute that draft" in suggestion["warnings"][0]
                assert "undotted" in suggestion["warnings"][0]
            else:
                assert "warnings" not in suggestion
        assert {
            column["name"] for column in describe_table(warehouse, '"sales.orders"')["columns"]
        } == {"id", "amount"}
        assert {
            column["name"] for column in describe_table(warehouse, "sales.orders")["columns"]
        } == {"id", "note"}
        declared = describe_table(warehouse, '"sales.v1"."orders.2026"')
        declared_suggestion = suggest_model(warehouse, '"sales.v1"."orders.2026"')
        inferred_suggestion = suggest_model(warehouse, '"sales.v1"."shipments.2026"')
    target = {"relation": '"sales.v1"."customers.2026"', "columns": ["customer_id"]}
    assert declared["foreign_keys"] == [{"columns": ["customer_id"], "references": target}]
    assert declared_suggestion["foreign_keys"][0]["references"] == target
    assert inferred_suggestion["foreign_keys"][0]["references"] == target

    server = create_architect_mcp_server(workspace_root=tmp_path)
    path = {"duckdb_path": db_path.name}
    calls = [("list_tables", path), ("list_tables", {**path, "schema": "sales.v1"})]
    for relation in expected:
        calls.extend(
            (tool, {**path, "relation": relation})
            for tool in ("describe_table", "profile_columns", "suggest_model")
        )
    results = _session(server, calls)
    assert {row["relation"] for row in results[0]["tables"]} == set(expected)
    assert {row["relation"] for row in results[1]["tables"]} == {
        relation for relation, (schema, _, _) in expected.items() if schema == "sales.v1"
    }
    mcp_by_relation = {}
    for index, relation in enumerate(expected):
        described, profiled, suggested = results[2 + index * 3 : 5 + index * 3]
        mcp_by_relation[relation] = (described, profiled, suggested)
        assert described["relation"] == profiled["relation"] == suggested["relation"] == relation
        assert profiled["columns"][0]["samples"] == [expected[relation][2]]
        assert suggested["upsert_model"]["relation"] == relation
        schema, name, _ = expected[relation]
        if "." in schema or "." in name:
            assert "cannot execute that draft" in suggested["warnings"][0]
            assert "undotted" in suggested["warnings"][0]
        else:
            assert "warnings" not in suggested
    assert {column["name"] for column in mcp_by_relation['"sales.orders"'][0]["columns"]} == {
        "id",
        "amount",
    }
    assert {column["name"] for column in mcp_by_relation["sales.orders"][0]["columns"]} == {
        "id",
        "note",
    }
    assert mcp_by_relation['"sales.v1"."orders.2026"'][0]["foreign_keys"][0]["references"] == target
    assert mcp_by_relation['"sales.v1"."orders.2026"'][2]["foreign_keys"][0]["references"] == target
    assert (
        mcp_by_relation['"sales.v1"."shipments.2026"'][2]["foreign_keys"][0]["references"] == target
    )


def test_dotted_relation_suggestion_reaches_upsert_model_draft_consumer(tmp_path: Path) -> None:
    project = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    db_path = build_dbt_warehouse(project / "data" / "warehouse.duckdb")
    with duckdb.connect(str(db_path)) as conn:
        conn.execute('CREATE TABLE main."sales.orders" (id INTEGER PRIMARY KEY, amount INTEGER)')
        conn.execute('INSERT INTO main."sales.orders" VALUES (11, 110)')

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, '"sales.orders"')
        draft = suggested["upsert_model"]
    mutation = ArchitectProject(project, workspace_root=tmp_path).upsert_model(**draft)

    assert mutation.report["ok"] is True, mutation.report
    authored = yaml.safe_load((project / "models" / "core" / "sales_orders.yml").read_text())
    assert authored["model"]["relation"] == draft["relation"] == '"sales.orders"'
    assert "cannot execute that draft" in suggested["warnings"][0]


@pytest.mark.parametrize(
    ("schema", "table", "relation", "draft_relation"),
    [
        ("main", "sales orders", '"sales orders"', "sales orders"),
        ("sales-data", 'sales"orders', '"sales-data"."sales""orders"', 'sales-data.sales"orders'),
    ],
)
def test_legacy_raw_special_name_draft_remains_executable(
    tmp_path: Path, schema: str, table: str, relation: str, draft_relation: str
) -> None:
    project = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    db_path = build_dbt_warehouse(project / "data" / "warehouse.duckdb")
    with duckdb.connect(str(db_path)) as conn:
        if schema != "main":
            conn.execute(f'CREATE SCHEMA "{schema}"')
        quoted_table = table.replace('"', '""')
        source = f'"{schema}"."{quoted_table}"'
        conn.execute(f"CREATE TABLE {source} (id INTEGER PRIMARY KEY, amount INTEGER)")
        conn.execute(f"INSERT INTO {source} VALUES (11, 110)")

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, relation)
        draft = suggested["upsert_model"]
    mutation = ArchitectProject(project, workspace_root=tmp_path).upsert_model(**draft)
    assert mutation.report["ok"] is True, mutation.report
    assert suggested["relation"] == relation
    assert draft["relation"] == draft_relation
    assert "warnings" not in suggested
    runtime = Runtime.from_path(str(project))
    try:
        rows = runtime.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": "measure.shop.amount"}, "as": "amount"}],
                "limit": 5,
            }
        )["rows"]
    finally:
        runtime.close()
    assert rows == [{"amount": 110}]


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

    relation = '"sales-data"."fct""orders"'
    with open_duckdb(db_path) as warehouse:
        listed = _by(list_tables(warehouse), "relation")
        described = describe_table(warehouse, relation)
        profiled = profile_columns(warehouse, relation, ["net amount"])
        suggested = suggest_model(warehouse, relation)
        assert describe_table(warehouse, 'sales-data.fct"orders') == described

    assert listed[relation]["schema"] == "sales-data"
    assert listed[relation]["name"] == 'fct"orders'
    assert described["primary_key"] == ["order id"]
    assert profiled["row_count"] == 1
    assert profiled["columns"][0]["samples"] == ["12.50"]
    assert suggested["relation"] == relation
    assert suggested["upsert_model"]["relation"] == 'sales-data.fct"orders'
    server = create_architect_mcp_server(workspace_root=tmp_path)
    mcp_listed, mcp_described, mcp_profiled, mcp_suggested = _session(
        server,
        [
            ("list_tables", {"duckdb_path": str(db_path)}),
            ("describe_table", {"relation": relation, "duckdb_path": str(db_path)}),
            ("profile_columns", {"relation": relation, "duckdb_path": str(db_path)}),
            ("suggest_model", {"relation": relation, "duckdb_path": str(db_path)}),
        ],
    )
    assert relation in _by(mcp_listed["tables"], "relation")
    assert mcp_described["ok"] is True and mcp_described["primary_key"] == ["order id"]
    assert mcp_profiled["relation"] == relation and mcp_profiled["row_count"] == 1
    assert mcp_suggested["relation"] == relation
    assert mcp_suggested["upsert_model"]["relation"] == 'sales-data.fct"orders'


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


@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["below", "at", "above"])
def test_key_suggestion_states_what_was_checked_at_the_profile_cap(
    tmp_path: Path, offset: int
) -> None:
    rows = MAX_PROFILE_ROWS + offset
    db_path = tmp_path / "orders.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            f"CREATE TABLE fct_orders AS SELECT range AS order_id, 1 AS amount FROM range({rows})"
        )

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "fct_orders")

    key = suggested["primary_key"]
    assert key["columns"] == ["order_id"]
    assert suggested["upsert_model"]["primary_key"] == ["order_id"]
    if offset > 0:
        assert suggested["sampled"] is True
        assert key["confidence"] == "low"
        assert "sample" in key["reason"] and "confirmed" in key["reason"]
    else:
        assert suggested["sampled"] is False
        assert key["confidence"] == "high" and "every row" in key["reason"]


def test_declared_key_is_certain_above_the_profile_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "declared.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE fct_orders (order_id BIGINT PRIMARY KEY, amount INTEGER)")
        conn.execute(f"INSERT INTO fct_orders SELECT range, 1 FROM range({MAX_PROFILE_ROWS + 1})")

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "fct_orders")

    assert suggested["sampled"] is True
    assert suggested["primary_key"] == {
        "columns": ["order_id"],
        "confidence": "high",
        "reason": "declared PRIMARY KEY",
    }


@pytest.mark.parametrize(
    "expression",
    ["range % 1000", "CASE WHEN range % 2 = 0 THEN NULL ELSE range END"],
    ids=["duplicates", "nulls"],
)
def test_sampled_duplicate_or_null_key_is_not_suggested(tmp_path: Path, expression: str) -> None:
    db_path = tmp_path / "invalid-key.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            f"CREATE TABLE fct_orders AS SELECT {expression} AS order_id, 1 AS amount "
            f"FROM range({MAX_PROFILE_ROWS + 1})"
        )

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "fct_orders")

    assert suggested["sampled"] is True
    assert suggested["primary_key"] is None
    assert suggested["upsert_model"]["primary_key"] == []


@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["below", "at", "above"])
def test_composite_key_suggestion_has_bounded_and_honest_evidence(
    tmp_path: Path, offset: int
) -> None:
    rows = MAX_PROFILE_ROWS + offset
    db_path = tmp_path / "lines.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            f"CREATE TABLE fct_order_lines AS SELECT range // 2 AS order_id, "
            f"range % 2 AS line_number, 1 AS amount FROM range({rows})"
        )

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "fct_order_lines")

    key = suggested["primary_key"]
    assert key["columns"] == ["order_id", "line_number"]
    assert suggested["upsert_model"]["primary_key"] == ["order_id", "line_number"]
    if offset > 0:
        assert key["confidence"] == "low"
        assert "first" in key["reason"] and "confirmed" in key["reason"]
    else:
        assert key["confidence"] == "medium"
        assert "every row" in key["reason"]


def test_composite_duplicate_outside_probe_remains_tentative(tmp_path: Path) -> None:
    db_path = tmp_path / "late-duplicate.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE fct_order_lines AS SELECT CASE WHEN range = 1000000 THEN 0 "
            "ELSE range // 2 END AS order_id, range % 2 AS line_number "
            f"FROM range({MAX_PROFILE_ROWS + 1})"
        )

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "fct_order_lines")

    assert suggested["primary_key"]["columns"] == ["order_id", "line_number"]
    assert suggested["primary_key"]["confidence"] == "low"
    assert "first 1000000" in suggested["primary_key"]["reason"]
    assert "must be confirmed" in suggested["primary_key"]["reason"]


def test_foreign_key_unmatched_outside_probe_requires_confirmation(tmp_path: Path) -> None:
    db_path = tmp_path / "late-orphan.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE dim_customers (customer_id BIGINT PRIMARY KEY)")
        conn.execute("INSERT INTO dim_customers VALUES (1)")
        conn.execute(
            "CREATE TABLE fct_orders AS SELECT range AS order_id, "
            "CASE WHEN range = 1000000 THEN 99 ELSE 1 END AS customer_id "
            f"FROM range({MAX_PROFILE_ROWS + 1})"
        )

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "fct_orders")

    link = _by(suggested["foreign_keys"], "column")["customer_id"]
    assert link["confidence"] == "low"
    assert "bounded prefix" in link["reason"]
    assert "must be confirmed" in link["reason"]


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


def test_external_data_view_cannot_be_profiled_or_suggested(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside.csv"
    sentinel = "SYNTHETIC_OUTSIDE_WORKSPACE"
    external.write_text(f"secret\n{sentinel}\n", encoding="utf-8")
    db_path = workspace / "warehouse.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE local_data AS SELECT 1 AS id, 'inside' AS value")
        conn.execute("CREATE VIEW local_view AS SELECT * FROM local_data")
        escaped = str(external).replace("'", "''")
        conn.execute(f"CREATE VIEW external_view AS SELECT secret FROM read_csv_auto('{escaped}')")

    with open_duckdb(db_path) as warehouse:
        tables = _by(list_tables(warehouse), "relation")
        described = describe_table(warehouse, "external_view")
        native_profile = profile_columns(warehouse, "local_view", ["value"])
        native_suggestion = suggest_model(warehouse, "local_view")
        for operation in (
            lambda: profile_columns(warehouse, "external_view"),
            lambda: suggest_model(warehouse, "external_view"),
        ):
            with pytest.raises(SemanticLayerError) as excinfo:
                operation()
            assert excinfo.value.code == "UNSUPPORTED_PLATFORM"
            assert excinfo.value.details == {"reason": "external_access_disabled"}
            assert sentinel not in str(excinfo.value) and str(external) not in str(excinfo.value)

    assert tables["external_view"]["kind"] == "view"
    assert described["columns"][0]["name"] == "secret"
    assert native_profile["columns"][0]["samples"] == ["inside"]
    assert native_suggestion["relation"] == "local_view"

    server = create_architect_mcp_server(workspace_root=workspace)
    path = {"duckdb_path": "warehouse.duckdb"}
    mcp_tables, mcp_described, external_profile, external_suggestion, local_profile = _session(
        server,
        [
            ("list_tables", path),
            ("describe_table", {**path, "relation": "external_view"}),
            ("profile_columns", {**path, "relation": "external_view"}),
            ("suggest_model", {**path, "relation": "external_view"}),
            ("profile_columns", {**path, "relation": "local_view"}),
        ],
    )
    assert mcp_tables["ok"] is True
    assert "external_view" in _by(mcp_tables["tables"], "relation")
    assert mcp_described["ok"] is True
    assert mcp_described["columns"][0]["name"] == "secret"
    for denied in (external_profile, external_suggestion):
        assert denied["ok"] is False
        assert denied["error"]["code"] == "UNSUPPORTED_PLATFORM"
        assert denied["error"]["details"] == {"reason": "external_access_disabled"}
        assert sentinel not in str(denied) and str(external) not in str(denied)
    assert local_profile["ok"] is True
    assert local_profile["columns"][1]["samples"] == ["inside"]


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
    links = orders["foreign_keys"]
    assert {
        link["references"]["relation"] for link in links if link["column"] == "customer_id"
    } == {
        "main_marts.dim_customers",
        "main_staging.stg_customers",
    }
    assert {link["references"]["relation"] for link in links if link["column"] == "store_id"} == {
        "main_marts.dim_stores",
        "main_staging.stg_stores",
    }
    assert all(link["confidence"] == "low" for link in links)


def test_suggest_model_for_a_line_table_finds_the_composite_key(warehouse_path: Path) -> None:
    with open_duckdb(warehouse_path) as warehouse:
        lines = suggest_model(warehouse, "main_marts.fct_order_lines")

    assert lines["primary_key"]["columns"] == ["order_id", "line_number"]
    links = lines["foreign_keys"]
    assert {link["references"]["relation"] for link in links if link["column"] == "order_id"} == {
        "main_marts.fct_orders",
        "main_staging.stg_orders",
    }
    assert {link["references"]["relation"] for link in links if link["column"] == "product_id"} == {
        "main_marts.dim_products",
        "main_staging.stg_products",
    }
    assert all(link["confidence"] == "low" for link in links)
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


@pytest.mark.parametrize(
    ("physical", "competitors", "measure_id", "expected"),
    [
        ("net-amount", "net INTEGER, amount INTEGER", "net_amount", 100),
        ("gross total", "gross INTEGER, total INTEGER", "gross_total", 200),
        ("fee.amount", "fee INTEGER, amount INTEGER", "fee_amount", 300),
        ("1+2", "one INTEGER, two INTEGER", "1_2", 400),
    ],
)
def test_special_physical_measure_names_execute_as_literal_columns(
    tmp_path: Path, physical: str, competitors: str, measure_id: str, expected: int
) -> None:
    project = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    db_path = build_dbt_warehouse(project / "data" / "warehouse.duckdb")
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            f'CREATE TABLE specials (id INTEGER PRIMARY KEY, "{physical}" INTEGER, {competitors})'
        )
        conn.execute(f"INSERT INTO specials VALUES (1, {expected}, 7, 2)")

    with open_duckdb(db_path) as warehouse:
        suggestion = suggest_model(warehouse, "specials")
    draft = suggestion["upsert_model"]
    assert draft["measures"][physical]["expr"] == {"kind": "column", "column": physical}
    mutation = ArchitectProject(project, workspace_root=tmp_path).upsert_model(**draft)
    assert mutation.report["ok"] is True, mutation.report
    runtime = Runtime.from_path(str(project))
    try:
        rows = runtime.query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"measure": f"measure.shop.{measure_id}"},
                        "as": "actual",
                    }
                ],
                "limit": 5,
            }
        )["rows"]
    finally:
        runtime.close()
    assert rows == [{"actual": expected}]


@pytest.mark.parametrize(
    ("name", "data_type", "expected"),
    [
        ("amount", "INTEGER", "measure"),
        ("amount", "DECIMAL(10,2)", "measure"),
        ("amount", "INT64", "measure"),
        ("amount", "DOUBLE PRECISION", "measure"),
        ("occurred_at", "TIMESTAMP_NTZ", "time"),
        ("occurred_on", "DATE", "time"),
        ("status", "VARCHAR(100)", "dimension"),
        ("status", "CHARACTER VARYING", "dimension"),
        ("customer_id", "INTEGER", "key"),
        ("weights", "INTEGER[]", "unknown"),
        ("weights", "INTEGER[3]", "unknown"),
        ("weights", "LIST(INTEGER)", "unknown"),
        ("weights", "ARRAY<INT64>", "unknown"),
        ("attrs", "STRUCT(score INTEGER)", "unknown"),
        ("lookup", "MAP(VARCHAR,INTEGER)", "unknown"),
        ("attrs", "ROW(score INT)", "unknown"),
        ("payload", "JSON", "unknown"),
        ("payload", "VARIANT", "unknown"),
        ("position", "POINT", "unknown"),
        ("duration", "INTERVAL", "unknown"),
        ("category", "ENUM('MAP', 'ARRAY', 'ok')", "dimension"),
        ("label", "ENUM('x[]', 'it''s MAP[] DATE INTEGER', 'ok')", "dimension"),
        ("label", "ENUM('TIMESTAMP', 'DECIMAL(10,2)', 'ok')", "dimension"),
        ("labels", "ENUM('MAP', 'ok')[]", "unknown"),
    ],
)
def test_scalar_classifier_does_not_promote_nested_type_words(
    name: str, data_type: str, expected: str
) -> None:
    assert classify_column(name, data_type) == expected


@pytest.mark.parametrize("incompatible_count", [1, 9])
def test_incompatible_foreign_key_targets_do_not_erase_valid_suggestion(
    tmp_path: Path, incompatible_count: int
) -> None:
    db_path = tmp_path / "types.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO customers VALUES (1), (2)")
        conn.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER)")
        conn.execute("INSERT INTO orders VALUES (10, 1), (20, 2)")
        for index in range(incompatible_count):
            conn.execute(f"CREATE TABLE unrelated_{index} (customer_id VARCHAR PRIMARY KEY)")
            conn.execute(f"INSERT INTO unrelated_{index} VALUES ('not-a-number')")

    with open_duckdb(db_path) as warehouse:
        direct = suggest_model(warehouse, "orders")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp,) = _session(
        server,
        [("suggest_model", {"duckdb_path": "types.duckdb", "relation": "orders"})],
    )

    for result in (direct, mcp):
        assert result.get("ok", True) is True
        assert [
            (link["references"]["relation"], link["confidence"]) for link in result["foreign_keys"]
        ] == [("customers", "high")]
        assert len(result["foreign_key_diagnostics"]) == incompatible_count
        assert all(
            "INTEGER versus VARCHAR" in diagnostic["reason"]
            for diagnostic in result["foreign_key_diagnostics"]
        )
        assert result["upsert_model"]["primary_key"] == ["order_id"]


def test_failed_individual_fk_probe_leaves_valid_link_with_incomplete_evidence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "probe.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO customers VALUES (1)")
        conn.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER)")
        conn.execute("INSERT INTO orders VALUES (10, 1)")
        conn.execute(
            "CREATE VIEW broken_customers AS SELECT CAST(value AS INTEGER) AS customer_id "
            "FROM (VALUES ('not-a-number')) AS source(value)"
        )

    with open_duckdb(db_path) as warehouse:
        direct = suggest_model(warehouse, "orders")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp,) = _session(
        server,
        [("suggest_model", {"duckdb_path": "probe.duckdb", "relation": "orders"})],
    )

    for result in (direct, mcp):
        assert result.get("ok", True) is True
        assert [link["references"]["relation"] for link in result["foreign_keys"]] == ["customers"]
        assert result["foreign_keys"][0]["confidence"] == "low"
        assert "another candidate could not be checked" in result["foreign_keys"][0]["reason"]
        assert result["foreign_key_diagnostics"] == [
            {
                "column": "customer_id",
                "relation": "broken_customers",
                "reason": "candidate probe is unsupported",
            }
        ]


@pytest.mark.parametrize("target_kind", ["undeclared_large", "declared_large", "checked_unique"])
def test_oversized_undeclared_fk_target_keeps_search_uncertainty(
    tmp_path: Path, target_kind: str
) -> None:
    db_path = tmp_path / "target-bound.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE a_customers (customer_id BIGINT PRIMARY KEY)")
        conn.execute("INSERT INTO a_customers VALUES (1), (2)")
        conn.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id BIGINT)")
        conn.execute("INSERT INTO orders VALUES (10, 1), (20, 2)")
        if target_kind == "declared_large":
            conn.execute("CREATE TABLE b_customers (customer_id BIGINT PRIMARY KEY)")
            conn.execute(f"INSERT INTO b_customers SELECT range FROM range({MAX_PROFILE_ROWS + 1})")
        elif target_kind == "undeclared_large":
            conn.execute(
                "CREATE VIEW b_customers AS SELECT range AS customer_id "
                f"FROM range({MAX_PROFILE_ROWS + 1})"
            )
        else:
            conn.execute("CREATE VIEW b_customers AS SELECT range AS customer_id FROM range(1, 3)")

    with open_duckdb(db_path) as warehouse:
        direct = suggest_model(warehouse, "orders")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp,) = _session(
        server,
        [("suggest_model", {"duckdb_path": "target-bound.duckdb", "relation": "orders"})],
    )

    for result in (direct, mcp):
        assert result.get("ok", True) is True
        links = (
            _by(result["foreign_keys"], "column")
            if target_kind == "undeclared_large"
            else result["foreign_keys"]
        )
        if target_kind == "undeclared_large":
            assert links["customer_id"]["references"]["relation"] == "a_customers"
            assert links["customer_id"]["confidence"] == "low"
            assert "another candidate could not be checked" in links["customer_id"]["reason"]
            assert result["foreign_key_diagnostics"] == [
                {
                    "column": "customer_id",
                    "relation": "b_customers",
                    "reason": "undeclared target exceeds the bounded uniqueness probe",
                }
            ]
        else:
            assert {link["references"]["relation"] for link in links} == {
                "a_customers",
                "b_customers",
            }
            assert all(link["confidence"] == "low" for link in links)
            assert "foreign_key_diagnostics" not in result
            if target_kind == "declared_large":
                assert any("values were not checked" in link["reason"] for link in links)
            else:
                assert all("every value here matches" in link["reason"] for link in links)


def test_container_columns_are_diagnosed_and_scalar_draft_executes(tmp_path: Path) -> None:
    project = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    db_path = build_dbt_warehouse(project / "data" / "warehouse.duckdb")
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE features (id INTEGER PRIMARY KEY, amount DECIMAL(10,2), "
            "weights INTEGER[], attrs STRUCT(score INTEGER), lookup MAP(VARCHAR, INTEGER), "
            "tags VARCHAR[], recorded_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO features VALUES "
            "(1, 10.50, [1,2], {'score': 3}, MAP(['a'], [1]), ['x'], TIMESTAMP '2024-01-01'), "
            "(2, 2.00, [4], {'score': 5}, MAP(['b'], [2]), ['y'], TIMESTAMP '2024-01-02')"
        )

    with open_duckdb(db_path) as warehouse:
        direct = suggest_model(warehouse, "features")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp,) = _session(
        server,
        [("suggest_model", {"duckdb_path": "shop/data/warehouse.duckdb", "relation": "features"})],
    )

    for result in (direct, mcp):
        assert result.get("ok", True) is True
        assert {row["column"] for row in result["unsupported_columns"]} == {
            "weights",
            "attrs",
            "lookup",
            "tags",
        }
        assert all("explicit extraction" in row["reason"] for row in result["unsupported_columns"])
        assert set(result["upsert_model"]["measures"]) == {"feature_count", "amount"}
        assert "recorded_at" in result["upsert_model"]["times"]
        assert not {"weights", "attrs", "lookup", "tags"} & set(
            result["upsert_model"]["dimensions"]
        )

    mutation = ArchitectProject(project, workspace_root=tmp_path).upsert_model(
        **direct["upsert_model"]
    )
    assert mutation.report["ok"] is True, mutation.report
    runtime = Runtime.from_path(str(project))
    try:
        rows = runtime.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": "measure.shop.amount"}, "as": "amount"}],
                "limit": 5,
            }
        )["rows"]
    finally:
        runtime.close()
    assert len(rows) == 1 and float(rows[0]["amount"]) == 12.5


def test_key_named_container_is_diagnosed_without_inferred_entity_key(tmp_path: Path) -> None:
    db_path = tmp_path / "container-key.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE events AS SELECT [1] AS event_id, 5 AS amount UNION ALL SELECT [2], 5"
        )

    with open_duckdb(db_path) as warehouse:
        suggested = suggest_model(warehouse, "events")

    assert suggested["primary_key"] is None
    assert suggested["upsert_model"]["primary_key"] == []
    assert suggested["unsupported_columns"] == [
        {
            "column": "event_id",
            "type": "INTEGER[]",
            "reason": "container type requires an explicit extraction expression",
        }
    ]


def test_enum_labels_with_container_words_and_brackets_stay_scalar_in_service_and_mcp(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "enum-types.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE enum_events (id INTEGER PRIMARY KEY, "
            "category ENUM('MAP', 'ARRAY', 'ok'), "
            "label ENUM('x[]', 'it''s MAP[] DATE INTEGER', 'ok'), "
            "amount DECIMAL(10,2), weights INTEGER[])"
        )
        conn.execute(
            "INSERT INTO enum_events VALUES "
            "(1, 'MAP', 'x[]', 10.5, [1,2]), "
            "(2, 'ARRAY', 'it''s MAP[] DATE INTEGER', 2.0, [3])"
        )

    with open_duckdb(db_path) as warehouse:
        described = describe_table(warehouse, "enum_events")
        direct = suggest_model(warehouse, "enum_events")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (mcp,) = _session(
        server,
        [("suggest_model", {"duckdb_path": "enum-types.duckdb", "relation": "enum_events"})],
    )

    types = _by(described["columns"], "name")
    assert types["category"]["type"] == "ENUM('MAP', 'ARRAY', 'ok')"
    assert types["label"]["type"] == "ENUM('x[]', 'it''s MAP[] DATE INTEGER', 'ok')"
    for result in (direct, mcp):
        assert result.get("ok", True) is True
        assert {row["column"] for row in result["dimensions"]} == {"category", "label"}
        assert set(result["upsert_model"]["dimensions"]) == {"category", "label"}
        assert {row["key"] for row in result["measures"]} == {"enum_event_count", "amount"}
        assert [row["column"] for row in result["unsupported_columns"]] == ["weights"]


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
