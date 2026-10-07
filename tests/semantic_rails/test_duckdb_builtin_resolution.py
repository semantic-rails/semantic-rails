"""Engine utility SQL cannot resolve macros stored in a database file."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from semantic_rails.architect_introspection import (
    describe_table,
    list_tables,
    open_duckdb,
    profile_columns,
    suggest_model,
)
from semantic_rails.db import Database, DuckDBAdapter, _build_csv_seed


@pytest.mark.parametrize("confined", [False, True], ids=["plain", "confined"])
def test_adapter_settings_ignore_file_macro(tmp_path: Path, confined: bool) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE MACRO current_setting(x) AS 'filter_pushdown'")
        assert connection.execute("SELECT current_setting('disabled_optimizers')").fetchone() == (
            "filter_pushdown",
        )
        connection.execute("CREATE TABLE orders AS SELECT 42 AS amount")

    adapter = DuckDBAdapter(str(path), confine_to=tmp_path if confined else "")
    try:
        connection = adapter._db.conn  # noqa: SLF001

        def setting(name: str):
            return connection.execute("SELECT system.main.current_setting(?)", [name]).fetchone()[0]

        assert setting("disabled_optimizers") == "common_subplan"
        assert adapter.query("SELECT amount FROM orders") == [{"amount": 42}]
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


@pytest.mark.parametrize(
    ("name", "arguments", "table_macro"),
    [
        ("duckdb_tables", "", True),
        ("duckdb_views", "", True),
        ("duckdb_columns", "", True),
        ("duckdb_constraints", "", True),
        ("current_database", "", False),
        ("count", "x", False),
        ("min", "x", False),
        ("max", "x", False),
        ("len", "x", False),
    ],
    ids=lambda value: str(value),
)
def test_introspection_ignores_file_macros(
    tmp_path: Path, name: str, arguments: str, table_macro: bool
) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE customers(customer_id INT PRIMARY KEY)")
        connection.execute("CREATE TABLE orders(order_id INT, customer_id INT, amount INT)")
        connection.execute("INSERT INTO orders VALUES (1, 1, 10), (2, 1, 20)")
        connection.execute("CREATE VIEW order_view AS SELECT * FROM orders")
        body = "TABLE SELECT 'file_macro' AS sentinel" if table_macro else "'file_macro'"
        connection.execute(f"CREATE MACRO {name}({arguments}) AS {body}")
        probe = (
            f"SELECT * FROM {name}()"
            if table_macro
            else f"SELECT {name}({'1' if arguments else ''})"
        )
        assert connection.execute(probe).fetchone() == ("file_macro",)

    with open_duckdb(path) as warehouse:
        assert {row["relation"] for row in list_tables(warehouse)} == {
            "customers",
            "orders",
            "order_view",
        }
        assert describe_table(warehouse, "customers")["primary_key"] == ["customer_id"]
        assert describe_table(warehouse, "order_view")["kind"] == "view"
        profile = profile_columns(warehouse, "orders")
        assert profile["row_count"] == 2
        amount = next(column for column in profile["columns"] if column["name"] == "amount")
        assert amount["min"] == 10
        assert amount["max"] == 20
        assert suggest_model(warehouse, "orders")["foreign_keys"]


def test_csv_seed_ignores_file_table_macro(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "orders.csv").write_text("amount\n42\n", encoding="utf-8")
    database = Database.connect(str(path))
    try:
        database.execute_script(
            "CREATE MACRO read_csv_auto(path, HEADER := false, NULLSTR := ['']) "
            "AS TABLE SELECT 'file_macro' AS sentinel"
        )
        assert database.query("SELECT * FROM read_csv_auto('unused')") == [
            {"sentinel": "file_macro"}
        ]
        _build_csv_seed(database, str(csv_dir), "", None)
        assert database.query("SELECT amount FROM orders") == [{"amount": 42}]
    finally:
        database.close()
