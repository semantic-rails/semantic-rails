"""Unit tests for the shared adapter helpers in db_parts.common."""

from __future__ import annotations

import pytest

from semantic_rails.db import Database, _split_sql_statements, load_csv_dir_to_duckdb, seed_db
from semantic_rails.db_parts.common import option_or_env, secret_value
from semantic_rails.errors import SemanticLayerError
from semantic_rails.sql_preparation import (
    float_nullif_divisions,
    map_double_quoted_identifiers,
    rewrite_double_quoted_identifiers,
)


def test_secret_files_strip_surrounding_whitespace(tmp_path):
    secret_file = tmp_path / "secret"
    for contents, expected in [
        (b"\t synthetic-file-secret \r\n\n", "synthetic-file-secret"),
        (b"\t synthetic\r\nfile-secret \r\n", "synthetic\nfile-secret"),
    ]:
        secret_file.write_bytes(contents)
        for option_name in ("password", "private_key", "token"):
            assert secret_value(option_name, "", str(secret_file)) == expected


def test_seed_splitting_keeps_statement_text_and_ignores_comments_quotes_and_semicolons():
    cases = [
        (f"{comment} SELECT 'a;b'; SELECT 2;", [f"{comment} SELECT 'a;b'", " SELECT 2"])
        for comment in [
            "-- the base table's key;\n",
            "-- the base table's key;\r\n",
            "/* the base table's key; */",
            "/* outer /* '; */ inner */",
        ]
    ]
    cases.append(
        (
            "SELECT/**/1; SELECT '-- /* ; */' AS \"a;b\"; -- tail",
            ["SELECT/**/1", " SELECT '-- /* ; */' AS \"a;b\""],
        )
    )
    cases += [
        (statement + "; /* keep */ SELECT 2; -- tail", [statement, " /* keep */ SELECT 2"])
        for statement in [
            r" SELECT e'it\'s; /*x*/ --x' AS value",
            " SELECT 'it''s; /*x*/' AS value",
            ' SELECT 1 AS "a"";b"',
            " SELECT 1 AS account$tag$name",
            " SELECT $é$a; /*x*/ --x$é$ AS value",
        ]
    ]
    for script, statements in cases:
        assert _split_sql_statements(script) == statements, script


def test_seed_dollar_quoted_and_escape_string_values_execute_with_data_intact():
    scripts = [
        (
            "-- the table's value;\nCREATE TABLE seed_example AS SELECT "
            f"${tag}${value}${tag}$ AS value; /* tail's ; */ SELECT 2;",
            value,
        )
        for tag in ["", "tag"]
        for value in ["a/*b*/c", "a--b", "a; ' /* $other$ --\nb"]
    ]
    for value in ["/*x*/", "--x", "; /*x*/ --x"]:
        statement = rf"CREATE TABLE seed_example AS SELECT E'it\'s {value}' AS value"
        assert _split_sql_statements(statement) == [statement]
        scripts.append((statement + "; SELECT 2;", f"it's {value}"))
    for script, value in scripts:
        db = Database.connect(":memory:")
        try:
            db.execute_script(script)
            assert db.query("SELECT value FROM seed_example") == [{"value": value}]
        finally:
            db.close()


def _refusal_before_any_execution(db, script):
    with pytest.raises(SemanticLayerError) as caught:
        db.execute_script(script)
    assert caught.value.code == "INVALID_CONFIG"
    sqlite = db.engine == "sqlite"
    catalog, field = (
        ("sqlite_master", "name") if sqlite else ("information_schema.tables", "table_name")
    )
    assert db.query(f"SELECT {field} FROM {catalog} WHERE {field} = 'seed_example'") == []
    return caught.value.details


def test_unclosed_seed_construct_refuses_before_any_execution():
    for fragment in [
        "$$a/*b*/c",
        "$tag$a--b$other$",
        "/* unterminated",
        "/* outer /* inner */",
        "'unterminated",
        '"unterminated',
        r"E'x\'",
    ]:
        db = Database.connect(":memory:")
        try:
            script = f"CREATE TABLE seed_example(value TEXT); SELECT {fragment}"
            assert _refusal_before_any_execution(db, script) == {
                "reason": "unterminated_sql_script"
            }
        finally:
            db.close()


def test_bare_carriage_return_refuses_before_any_execution():
    for engine in ["duckdb", "sqlite"]:
        for tail in [
            "-- comment\rINSERT INTO seed_example VALUES (7);",
            "-- comment\rSELECT 'unterminated",
        ]:
            db = Database.connect(":memory:", engine=engine)
            try:
                script = f"CREATE TABLE seed_example(value INTEGER); {tail}"
                details = _refusal_before_any_execution(db, script)
                assert details["reason"] == "bare_carriage_return_sql_script"
            finally:
                db.close()
    for script in ["SELECT 1; -- tail\r", "SELECT 'a\rb';", "-- ok\r\nSELECT 1;\r"]:
        with pytest.raises(SemanticLayerError) as caught:
            _split_sql_statements(script)
        assert caught.value.code == "INVALID_CONFIG"
        assert caught.value.details["reason"] == "bare_carriage_return_sql_script"


@pytest.mark.parametrize("seed_kind", ["sql_script", "csv_dir_duckdb"])
def test_seed_file_refuses_bare_carriage_return_and_executes_crlf(tmp_path, seed_kind):
    def seed(source, target):
        if seed_kind == "sql_script":
            seed_db(str(target), str(source))
        else:
            load_csv_dir_to_duckdb(str(target), str(tmp_path), str(source))

    source = tmp_path / "seed.sql"
    source.write_bytes(
        b"CREATE TABLE seed_example(value INTEGER); -- comment\rINSERT INTO seed_example VALUES (7);"
    )
    target = tmp_path / "seed.duckdb"
    with pytest.raises(SemanticLayerError) as caught:
        seed(source, target)
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.details == {
        "reason": "bare_carriage_return_sql_script",
        "file": str(source),
    }
    assert str(source) in str(caught.value)
    assert not target.exists()

    source.write_bytes(
        b"CREATE TABLE seed_example(value INTEGER); -- comment\r\n"
        b"INSERT INTO seed_example VALUES (7); -- second\r\n"
        b"INSERT INTO seed_example VALUES (9); -- tail\r\n"
    )
    seed(source, target)
    db = Database.connect(str(target))
    try:
        assert db.query("SELECT value FROM seed_example ORDER BY value") == [
            {"value": 7},
            {"value": 9},
        ]
    finally:
        db.close()


def test_double_quoted_identifiers_rewrite_to_backticks_and_literals_stay():
    for sql, expected in [
        (
            'SELECT base.g1 AS "dimension.jaffle_product_type" FROM t ORDER BY "g1"',
            "SELECT base.g1 AS `dimension.jaffle_product_type` FROM t ORDER BY `g1`",
        ),
        (
            'SELECT \'a "quoted" word\' AS "alias", \'\' AS "empty" FROM t',
            "SELECT 'a \"quoted\" word' AS `alias`, '' AS `empty` FROM t",
        ),
        # '' inside a string stays; "" inside an identifier collapses to one ".
        (
            'SELECT \'it\'\'s "fine"\' AS "odd""name" FROM t',
            "SELECT 'it''s \"fine\"' AS `odd\"name` FROM t",
        ),
        ("SELECT a, 'b' FROM t WHERE c = 'd''e'", "SELECT a, 'b' FROM t WHERE c = 'd''e'"),
    ]:
        assert rewrite_double_quoted_identifiers(sql) == expected

    # The scanning core passes the unescaped inner text and emits the replacement verbatim.
    seen: list[str] = []

    def upper(name: str) -> str:
        seen.append(name)
        return f"[{name.upper()}]"

    assert map_double_quoted_identifiers('SELECT 1 AS "a""b", \'keep "this"\' AS note', upper) == (
        'SELECT 1 AS [A"B], \'keep "this"\' AS note'
    )
    assert seen == ['a"b']


def test_float_nullif_divisions_casts_the_guard_outside_literals():
    plain = "SELECT a / NULLIF(b, 0) AS r FROM t"
    for sql, cast_type, expected in [
        (plain, "DOUBLE", "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) AS r FROM t"),
        (
            plain,
            "DOUBLE PRECISION",
            "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE PRECISION) AS r FROM t",
        ),
        (
            "SELECT a / NULLIF(b / NULLIF(c, 0), 0) AS r, ' / NULLIF(' AS note FROM t",
            "DOUBLE",
            "SELECT a / CAST(NULLIF(b / CAST(NULLIF(c, 0) AS DOUBLE), 0) AS DOUBLE) AS r, "
            "' / NULLIF(' AS note FROM t",
        ),
        (
            "SELECT 'a / NULLIF(b, 0)' AS doc, c / NULLIF(d, 0) FROM t",
            "DOUBLE",
            "SELECT 'a / NULLIF(b, 0)' AS doc, c / CAST(NULLIF(d, 0) AS DOUBLE) FROM t",
        ),
        (
            "SELECT a / NULLIF(SUM(CASE WHEN s = ')''(' THEN n END), 0) FROM t",
            "DOUBLE",
            "SELECT a / CAST(NULLIF(SUM(CASE WHEN s = ')''(' THEN n END), 0) AS DOUBLE) FROM t",
        ),
        # An unbalanced tail and a plain division stay as they are.
        ("SELECT a / NULLIF(b, 0 FROM t", "DOUBLE", "SELECT a / NULLIF(b, 0 FROM t"),
        ("SELECT a / NULLIF(b, 0", "DOUBLE", "SELECT a / NULLIF(b, 0"),
        ("SELECT a / b, NULLIF(c, 0) FROM t", "DOUBLE", "SELECT a / b, NULLIF(c, 0) FROM t"),
    ]:
        assert float_nullif_divisions(sql, cast_type=cast_type) == expected


def test_option_or_env_prefers_the_literal_then_the_env(monkeypatch):
    monkeypatch.setenv("SR_TEST_HOST", "from-env")
    assert option_or_env({"host": "literal", "host_env": "SR_TEST_HOST"}, "host") == "literal"
    assert option_or_env({"host_env": "SR_TEST_HOST"}, "host") == "from-env"

    monkeypatch.delenv("SR_TEST_HOST", raising=False)
    missing: list[str] = []
    assert option_or_env({"host_env": "SR_TEST_HOST"}, "host", missing) == ""
    assert missing == ["SR_TEST_HOST"]

    # No literal and no *_env option: empty, and nothing recorded.
    missing = []
    assert option_or_env({}, "host", missing) == ""
    assert missing == []


def test_backslash_quoted_literal_survives_preparation():
    # BigQuery reads '' inside a literal as two literals and Spark as two concatenated
    # ones, so a quote is \' there; the passes must not end the literal at it.
    from semantic_rails.dialects import dialect_for_warehouse
    from semantic_rails.renderer import render_expr, use_dialect
    from semantic_rails.sql_ast import SqlBinary, SqlIdentifier, SqlLiteral

    value = 'it\'s "b" / NULLIF(c, 0) \\'
    for warehouse in ["bigquery", "databricks"]:
        dialect = dialect_for_warehouse(warehouse)
        with use_dialect(dialect):
            literal = render_expr(SqlBinary(SqlIdentifier(["t", "s"]), "=", SqlLiteral(value)))
        assert literal == "t.s = 'it\\'s \"b\" / NULLIF(c, 0) \\\\'"
        sql = f'SELECT a / NULLIF(b, 0) AS "x" FROM t WHERE {literal}'
        ratio = "CAST(NULLIF(b, 0) AS DOUBLE)" if warehouse == "databricks" else "NULLIF(b, 0)"
        assert dialect.prepare_query(sql).sql == f"SELECT a / {ratio} AS `x` FROM t WHERE {literal}"
