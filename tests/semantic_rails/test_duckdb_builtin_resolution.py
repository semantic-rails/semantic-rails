"""Engine utility SQL cannot resolve macros stored in a database file."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from semantic_rails.architect_introspection import (
    DuckDBWarehouse,
    describe_table,
    list_tables,
    open_duckdb,
    profile_columns,
    suggest_model,
)
from semantic_rails.db import Database, DuckDBAdapter, _build_csv_seed
from semantic_rails.db_parts.duckdb_setup import configure_duckdb_connection
from semantic_rails.db_parts.ducklake import DuckLakeAdapter
from semantic_rails.errors import SemanticLayerError


@pytest.mark.parametrize("confined", [False, True], ids=["plain", "confined"])
def test_adapter_settings_with_non_colliding_file_macro(tmp_path: Path, confined: bool) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE MACRO order_amount(x) AS x")
        connection.execute("CREATE MACRO order_rows() AS TABLE SELECT 42 AS amount")
        connection.execute("CREATE TABLE orders AS SELECT 42 AS amount")

    adapter = DuckDBAdapter(str(path), confine_to=tmp_path if confined else "")
    try:
        connection = adapter._db.conn  # noqa: SLF001

        def setting(name: str):
            return connection.execute("SELECT system.main.current_setting(?)", [name]).fetchone()[0]

        assert setting("disabled_optimizers") == "common_subplan"
        assert adapter.query("SELECT order_amount(amount) AS amount FROM orders") == [
            {"amount": 42}
        ]
        assert adapter.query("SELECT * FROM order_rows()") == [{"amount": 42}]
        if confined:
            for name, expected in {
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "lock_configuration": True,
                "allowed_configs": ["TimeZone"],
            }.items():
                assert setting(name) == expected
            directory = os.path.realpath(tmp_path)
            assert directory + os.sep in setting("allowed_directories")
            assert all(
                os.path.commonpath([entry, directory]) == directory
                for entry in setting("allowed_directories")
            )
            assert all(
                os.path.commonpath([entry, directory]) == directory
                for entry in setting("allowed_paths")
            )
            assert os.path.commonpath([setting("temp_directory"), str(tmp_path)]) == str(tmp_path)
    finally:
        adapter.close()
    with open_duckdb(path) as warehouse:
        assert profile_columns(warehouse, "orders")["row_count"] == 1


@pytest.mark.parametrize(
    ("name", "arguments", "table_macro", "probe"),
    [
        ("duckdb_tables", "", True, "SELECT * FROM duckdb_tables()"),
        ("duckdb_views", "", True, "SELECT * FROM duckdb_views()"),
        ("duckdb_columns", "", True, "SELECT * FROM duckdb_columns()"),
        ("duckdb_constraints", "", True, "SELECT * FROM duckdb_constraints()"),
        ("current_database", "", False, "SELECT current_database()"),
        ("current_setting", "x", False, "SELECT current_setting('disabled_optimizers')"),
        ("count", "x", False, "SELECT count(1)"),
        ("min", "x", False, "SELECT min(1)"),
        ("max", "x", False, "SELECT max(1)"),
        ("len", "x", False, "SELECT len('a')"),
        ("-", "x, y", False, "SELECT 2 - 1"),
        ("row", "x, y", False, "SELECT (1, 2)"),
        ("array_extract", "x, y", False, "SELECT [1, 2][1]"),
    ],
    ids=lambda value: str(value),
)
def test_introspection_ignores_file_macros(
    tmp_path: Path, name: str, arguments: str, table_macro: bool, probe: str
) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE customers(customer_id INT PRIMARY KEY)")
        connection.execute("CREATE TABLE orders(order_id INT, customer_id INT, amount INT)")
        connection.execute("INSERT INTO orders VALUES (1, 1, 10), (2, 1, 20), (3, 1, NULL)")
        connection.execute("CREATE VIEW order_view AS SELECT * FROM orders")
        connection.execute("CREATE TABLE order_lines(order_id INT, line_id INT)")
        connection.execute("INSERT INTO order_lines VALUES (1, 1), (1, 2), (2, 1), (2, 2)")
        body = "error('file_macro_executed')"
        if table_macro:
            body = f"TABLE SELECT {body} AS sentinel"
        connection.execute(f'CREATE MACRO "{name}"({arguments}) AS {body}')
        # Subscript macro lookup fails during binding in DuckDB 1.5.6.
        error = "Cannot copy bound expression" if name == "array_extract" else "file_macro_executed"
        with pytest.raises(duckdb.Error, match=error):
            connection.execute(probe).fetchall()

    with pytest.raises(SemanticLayerError) as exc, open_duckdb(path):
        pytest.fail("colliding file was accepted")
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {"reason": "duckdb_builtin_macro_collision", "macros": [name]}
    assert "file_macro_executed" not in str(exc.value)
    for confined in (False, True):
        with pytest.raises(SemanticLayerError) as exc:
            DuckDBAdapter(str(path), confine_to=tmp_path if confined else "")
        assert exc.value.details == {"reason": "duckdb_builtin_macro_collision", "macros": [name]}

    # Bypass the connect guard deliberately to verify qualified utility SQL
    # independently: the defense must still hold on an existing connection.
    with duckdb.connect(str(path), read_only=True) as connection:
        warehouse = DuckDBWarehouse(str(path), connection)
        assert {row["relation"] for row in list_tables(warehouse)} == {
            "customers",
            "orders",
            "order_view",
            "order_lines",
        }
        assert describe_table(warehouse, "customers")["primary_key"] == ["customer_id"]
        assert describe_table(warehouse, "order_view")["kind"] == "view"
        profile = profile_columns(warehouse, "orders")
        assert profile["row_count"] == 3
        amount = next(column for column in profile["columns"] if column["name"] == "amount")
        assert amount["min"] == 10
        assert amount["max"] == 20
        assert amount["null_count"] == 1
        assert suggest_model(warehouse, "orders")["foreign_keys"]
        assert suggest_model(warehouse, "order_lines")["primary_key"]["columns"] == [
            "order_id",
            "line_id",
        ]


@pytest.mark.parametrize(
    ("name", "arguments", "table_macro", "probe"),
    [
        ("+", "x, y", False, "SELECT 1 + 2"),
        ("*", "x, y", False, "SELECT 1 * 2"),
        ("/", "x, y", False, "SELECT 1 / 2"),
        ("%", "x, y", False, "SELECT 1 % 2"),
        ("||", "x, y", False, "SELECT 'a' || 'b'"),
        ("struct_pack", "a", False, "SELECT {'a': 1}"),
        ("list_value", "x, y", False, "SELECT [1, 2]"),
        ("~~", "x, y", False, "SELECT 'a' LIKE 'b'"),
        ("~~*", "x, y", False, "SELECT 'a' ILIKE 'b'"),
        ("struct_extract", "x, y", False, "SELECT ({'a': 1}).a"),
        ("DuCkDb_FuNcTiOnS", "", True, "SELECT * FROM duckdb_functions()"),
        ("read_csv_auto", "x", True, "SELECT * FROM read_csv_auto('unused')"),
        ("lower", "x", False, "SELECT lower('a')"),
        ("generate_subscripts", "x, y", False, "SELECT generate_subscripts([1], 1)"),
    ],
    ids=lambda value: str(value),
)
def test_connections_refuse_colliding_macros_before_setup(
    tmp_path: Path, name: str, arguments: str, table_macro: bool, probe: str
) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as connection:
        # Include another schema: collisions cannot hide outside main.
        connection.execute("CREATE SCHEMA authored")
        body = "error('file_macro_executed')"
        if table_macro:
            body = f"TABLE SELECT {body} AS sentinel"
        schema = "main" if name in {"list_value", "struct_pack"} else "authored"
        connection.execute(f'CREATE MACRO {schema}."{name}"({arguments}) AS {body}')
        connection.execute("SET schema = 'authored'")
        error = (
            "Cannot copy bound expression" if name == "struct_extract" else "file_macro_executed"
        )
        with pytest.raises(duckdb.Error, match=error):
            connection.execute(probe).fetchall()

    # Observe the real connection to prove refusal precedes settings and closes it.
    raw = duckdb.connect(str(path), read_only=True)
    statements = []

    class ObservedConnection:
        def execute(self, sql, parameters=None):
            statements.append(sql)
            return raw.execute(sql, parameters or [])

        def close(self):
            raw.close()

    with pytest.raises(SemanticLayerError) as exc:
        configure_duckdb_connection(ObservedConnection())
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {"reason": "duckdb_builtin_macro_collision", "macros": [name]}
    assert len(statements) == 1
    assert "system.main.duckdb_functions()" in statements[0]
    with pytest.raises(duckdb.ConnectionException, match="closed"):
        raw.execute("SELECT 1")
    for factory in (lambda: Database.connect(str(path)), lambda: DuckDBAdapter(str(path))):
        with pytest.raises(SemanticLayerError) as exc:
            factory()
        assert exc.value.details["reason"] == "duckdb_builtin_macro_collision"
        assert exc.value.details["macros"] == [name]


@pytest.mark.parametrize("name", ["read_csv_auto", "list_value"])
def test_csv_seed_ignores_file_macros_on_existing_connection(tmp_path: Path, name: str) -> None:
    path = tmp_path / "warehouse.duckdb"
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "orders.csv").write_text("amount\n42\n", encoding="utf-8")
    database = Database.connect(str(path))
    try:
        definition = (
            "read_csv_auto(path, HEADER := false, NULLSTR := ['']) "
            "AS TABLE SELECT error('file_macro_executed') AS sentinel"
            if name == "read_csv_auto"
            else "list_value(x) AS error('file_macro_executed')"
        )
        database.execute_script(f"CREATE MACRO {definition}")
        probe = "read_csv_auto('unused')" if name == "read_csv_auto" else "(SELECT [''])"
        with pytest.raises(duckdb.Error, match="file_macro_executed"):
            database.query(f"SELECT * FROM {probe}")
        _build_csv_seed(database, str(csv_dir), "", None)
        assert database.query("SELECT amount FROM orders") == [{"amount": 42}]
    finally:
        database.close()


def test_ducklake_refuses_macros_in_newly_attached_catalog(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE MACRO row(x, y) AS error('file_macro_executed')")
    raw = duckdb.connect()
    statements = []

    class Connection:
        def execute(self, sql, parameters=None):
            statements.append(sql)
            if sql.startswith(("INSTALL ", "LOAD ")):
                return self
            if sql.startswith("ATTACH "):
                # Simulate the attachment without installing the extension.
                escaped = str(path).replace("'", "''")
                return raw.execute(f"ATTACH '{escaped}' AS jaffle")
            return raw.execute(sql, parameters or [])

        def close(self):
            raw.close()

    monkeypatch.setattr(
        "semantic_rails.db_parts.ducklake.import_driver",
        lambda *a, **kw: SimpleNamespace(connect=lambda: Connection()),
    )
    adapter = DuckLakeAdapter({"catalog_path": str(path)})
    with pytest.raises(SemanticLayerError) as exc:
        adapter._create_connection()  # noqa: SLF001
    assert exc.value.details == {"reason": "duckdb_builtin_macro_collision", "macros": ["row"]}
    assert not any(sql.startswith("USE ") for sql in statements)
    with pytest.raises(duckdb.ConnectionException, match="closed"):
        raw.execute("SELECT 1")
