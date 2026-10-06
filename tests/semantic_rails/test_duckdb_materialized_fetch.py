"""DuckDB-family reads materialize relations and preserve DB-API result semantics."""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from semantic_rails import architect_introspection, db
from semantic_rails.db_parts import common
from semantic_rails.db_parts.ducklake import DuckLakeAdapter
from semantic_rails.db_parts.motherduck import MotherDuckAdapter
from semantic_rails.sql_preparation import ParameterSlot, PreparedQuery

WINDOW_QUERY = (
    "SELECT id, COUNT(id) OVER () AS total FROM (VALUES (3), (1), (2)) AS t(id) ORDER BY id"
)


class RecordingRelation:
    def __init__(self, raw: Any, log: list[Any]):
        self.raw = raw
        self.log = log

    @property
    def description(self):
        return self.raw.description

    def limit(self, size):
        self.log.append(("relation.limit", size))
        return RecordingRelation(self.raw.limit(size), self.log)

    def fetchall(self):
        self.log.append(("relation.fetchall",))
        return self.raw.fetchall()

    def fetchmany(self, *_args):
        raise AssertionError("relation.fetchmany opens a streamed result")


class RecordingConnection:
    def __init__(self, raw: Any, log: list[Any]):
        self.raw = raw
        self.log = log
        self.cursors: list[RecordingConnection] = []

    def cursor(self):
        cursor = RecordingConnection(self.raw.cursor(), self.log)
        self.cursors.append(cursor)
        return cursor

    def execute(self, sql, params=None):
        self.log.append(("cursor.execute", sql))
        self.raw.execute(sql, params or [])
        return self

    def sql(self, sql, *, params):
        self.log.append(("cursor.sql", sql, list(params)))
        relation = self.raw.sql(sql, params=params)
        return None if relation is None else RecordingRelation(relation, self.log)

    def extract_statements(self, sql):
        return self.raw.extract_statements(sql)

    @property
    def description(self):
        raise AssertionError("read the materialized relation's description")

    def fetchall(self):
        raise AssertionError("cursor.fetchall reads a streamed result")

    def fetchmany(self, *_args):
        raise AssertionError("cursor.fetchmany reads a streamed result")

    def fetchone(self):
        raise AssertionError("cursor.fetchone reads a streamed result")

    def close(self):
        for cursor in self.cursors:
            cursor.close()
        self.raw.close()


@pytest.fixture()
def helper_calls(monkeypatch):
    original = common.materialized_duckdb_result
    calls = []

    def recording_helper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    for module in (db, common, architect_introspection):
        monkeypatch.setattr(module, "materialized_duckdb_result", recording_helper)
    return calls


@pytest.mark.parametrize("path", ["database", "duckdb", "ducklake", "motherduck"])
@pytest.mark.parametrize("cap", [None, 1, 3, 5])
def test_every_duckdb_adapter_materializes_instead_of_streaming(path, cap, helper_calls):
    log = []
    connection = RecordingConnection(duckdb.connect(), log)
    try:
        if path == "database":
            rows = db.Database(connection, "duckdb").query(WINDOW_QUERY, max_rows=cap)
        else:
            if path == "duckdb":
                adapter = db.DuckDBAdapter.__new__(db.DuckDBAdapter)
                adapter._db = db.Database(connection, "duckdb")
            else:
                adapter = (
                    DuckLakeAdapter()
                    if path == "ducklake"
                    else MotherDuckAdapter({"database": "unused", "token_env": "UNUSED_TOKEN"})
                )
                adapter._conn = connection  # no extension, network or credential resolution
            rows = adapter.query(WINDOW_QUERY, limits={"max_rows": cap})
        expected = [{"id": n, "total": 3} for n in (1, 2, 3)]
        assert rows == expected[:cap]
        assert rows.truncated is (cap is not None and cap < len(expected))
        assert len(helper_calls) == 1
        assert log == [
            ("cursor.sql", WINDOW_QUERY, []),
            *([] if cap is None else [("relation.limit", cap + 1)]),
            ("relation.fetchall",),
        ]
    finally:
        connection.close()


@pytest.mark.parametrize("cap", [None, 2, 6, 10])
@pytest.mark.parametrize("bound", [False, True])
def test_jaffle_rows_order_description_duplicate_names_and_parameters(runtime_factory, cap, bound):
    runtime = runtime_factory("jaffle_shop")
    sql = (
        "SELECT order_id, item_id, COUNT(item_revenue_cents) OVER () AS total, "
        "item_revenue_cents AS value, item_id AS value "
        "FROM jaffle_item WHERE order_id >= "
        + ("?" if bound else "''")
        + " ORDER BY order_id, item_id LIMIT 6"
    )
    params = [""] if bound else []
    with duckdb.connect(runtime.db_path, read_only=True) as reference:
        cursor = reference.execute(sql, params)
        expected_description = cursor.description
        expected_rows = cursor.fetchall()
    log = []
    connection = RecordingConnection(duckdb.connect(runtime.db_path, read_only=True), log)
    try:
        description, fetched = common.materialized_duckdb_result(
            connection, sql, iter(params), max_rows=cap
        )
        assert description == expected_description
        assert fetched == expected_rows[: None if cap is None else cap + 1]
        expected = [
            {column[0]: row[i] for i, column in enumerate(expected_description)}
            for row in expected_rows
        ]
        rows = db.Database(connection, "duckdb").query(sql, iter(params), max_rows=cap)
        assert rows == expected[:cap]
        assert rows.truncated is (cap is not None and cap < len(expected))
        assert all(set(row) == {"order_id", "item_id", "total", "value"} for row in rows)
        executed_sql = (
            f"SELECT * FROM ({sql}\n) AS q LIMIT {cap + 1}" if bound and cap is not None else sql
        )
        assert ("cursor.sql", executed_sql, params) in log
    finally:
        connection.close()
        runtime.close()


@pytest.mark.timeout(10)
def test_parameterized_max_rows_bounds_materialization():
    sql = "SELECT i FROM range(?) t(i)"
    log = []
    connection = RecordingConnection(duckdb.connect(), log)
    try:
        rows = db.Database(connection, "duckdb").query(sql, [10**8], max_rows=2)
        assert rows == [{"i": 0}, {"i": 1}]
        assert rows.truncated is True
        # Binding executes eagerly: the uncapped statement must never run.
        assert ("cursor.sql", sql, [10**8]) not in log
        assert not any(entry[0] == "relation.limit" for entry in log)
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["database", "duckdb"])
@pytest.mark.parametrize("cap", [None, 1, 2])
@pytest.mark.parametrize("sql", ["EXPLAIN SELECT ? AS n", "DESCRIBE SELECT ? AS n"])
def test_parameterized_non_select_matches_dbapi(path, cap, sql):
    with duckdb.connect() as reference:
        cursor = reference.execute(sql, [42])
        expected = [
            dict(zip([column[0] for column in cursor.description], row, strict=True))
            for row in cursor.fetchall()
        ]
    log = []
    connection = RecordingConnection(duckdb.connect(), log)
    try:
        database = db.Database(connection, "duckdb")
        if path == "database":
            rows = database.query(sql, [42], max_rows=cap)
        else:
            adapter = db.DuckDBAdapter.__new__(db.DuckDBAdapter)
            adapter._db = database
            prepared = PreparedQuery(sql, parameters=(ParameterSlot("n", "integer"),))
            rows = adapter.query_prepared(prepared, parameters=[42], limits={"max_rows": cap})
        assert rows == expected[:cap]
        assert rows.truncated is (cap is not None and len(expected) > cap)
        assert [entry for entry in log if entry[0] == "cursor.sql"] == [("cursor.sql", sql, [42])]
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["database", "duckdb"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ? AS n;",
        "SELECT ? AS n; -- tail",
        "SELECT ? AS n;; -- tail",
        "SELECT ? AS n -- tail",
        "WITH t AS (SELECT ? AS n) SELECT n FROM t;",
        "SELECT ? AS n, 'é' AS x",
        "SELECT 'é' AS s, ? AS n;",
        "SELECT 'é' AS s, ? AS n;\n",
        "SELECT 'é' AS s, ? AS n; -- tail",
    ],
)
def test_parameterized_select_cap_preserves_statement_terminators_and_comments(path, sql):
    with duckdb.connect() as reference:
        cursor = reference.execute(sql, [42])
        columns = [column[0] for column in cursor.description]
        expected = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    database = db.Database.connect_in_memory()
    try:
        if path == "database":
            rows = database.query(sql, [42], max_rows=1)
        else:
            adapter = db.DuckDBAdapter.__new__(db.DuckDBAdapter)
            adapter._db = database
            prepared = PreparedQuery(sql, parameters=(ParameterSlot("n", "integer"),))
            rows = adapter.query_prepared(prepared, parameters=[42], limits={"max_rows": 1})
        assert rows == expected[:1]
        assert list(rows[0]) == columns
        assert rows.truncated is (len(expected) > 1)
    finally:
        database.close()


def test_non_select_returns_empty_rows_without_a_cursor_fetch():
    connection = RecordingConnection(duckdb.connect(), [])
    try:
        database = db.Database(connection, "duckdb")
        rows = database.query("CREATE TABLE empty_table(id INTEGER)", max_rows=1)
        assert rows == []
        assert rows.truncated is False
        assert database.query("SELECT count(*) AS n FROM empty_table") == [{"n": 0}]
    finally:
        connection.close()


def test_introspection_materializes_catalog_profile_sample_and_composite_key_reads(helper_calls):
    raw = duckdb.connect()
    raw.execute("CREATE TABLE items(order_id INT, item_id INT, amount INT)")
    raw.execute("INSERT INTO items VALUES (1, 1, 10), (1, 2, 20), (2, 1, 30)")
    log = []
    connection = RecordingConnection(raw, log)
    try:
        warehouse = architect_introspection.DuckDBWarehouse(":memory:", connection)
        assert warehouse.rows(WINDOW_QUERY) == [{"id": n, "total": 3} for n in (1, 2, 3)]
        assert architect_introspection.profile_columns(warehouse, "items")["row_count"] == 3
        suggested = architect_introspection.suggest_model(warehouse, "items")
        assert suggested["primary_key"]["columns"] == ["order_id", "item_id"]
        assert suggested["row_count"] == 3
        queries = [entry for entry in log if entry[0] == "cursor.sql"]
        assert len(helper_calls) > 1
        assert len(queries) == len([entry for entry in log if entry[0] == "relation.fetchall"])
        assert any("SELECT DISTINCT" in entry[1] for entry in queries)
        assert any("count(DISTINCT (" in entry[1] for entry in queries)
        assert not any(entry[0] == "cursor.execute" for entry in log)
    finally:
        connection.close()


@pytest.mark.parametrize("cap", [None, 1, 3])
def test_sqlite_retains_its_cursor_rows_and_duplicate_column_semantics(cap):
    database = db.Database.connect(":memory:", engine="sqlite")
    try:
        rows = database.query(
            "SELECT ? AS value, 2 AS value UNION ALL SELECT 3, 4", [1], max_rows=cap
        )
        assert rows == [{"value": 2}, {"value": 4}][:cap]
        assert rows.truncated is (cap is not None and cap < 2)
    finally:
        database.close()
