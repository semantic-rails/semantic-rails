"""Unit tests for the shared adapter helpers in db_parts.common."""

from __future__ import annotations

import pytest

from semantic_rails.db import Database, _split_sql_statements, load_csv_dir_to_duckdb, seed_db
from semantic_rails.db_parts.common import (
    option_or_env,
    timeout_option,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.sql_preparation import (
    float_nullif_divisions,
    map_double_quoted_identifiers,
    rewrite_double_quoted_identifiers,
)


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_timeout_option_rejects_nonpositive_or_invalid_values_without_echoing_them(value):
    with pytest.raises(SemanticLayerError) as exc:
        timeout_option(
            {"read_timeout_seconds": value},
            "read_timeout_seconds",
            65,
            engine="postgres",
            connection_kind="postgres_native",
        )
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["option"] == "read_timeout_seconds"
    assert value not in str(exc.value)


@pytest.mark.parametrize(
    "comment",
    [
        "-- the base table's key;\n",
        "-- the base table's key;\r\n",
        "/* the base table's key; */",
        "/* outer /* '; */ inner */",
    ],
)
def test_seed_splitting_ignores_comment_quotes_and_semicolons(comment):
    assert _split_sql_statements(f"{comment} SELECT 'a;b'; SELECT 2;") == [
        f"{comment} SELECT 'a;b'",
        " SELECT 2",
    ]


def test_seed_splitting_preserves_quoted_comments_and_token_boundaries():
    assert _split_sql_statements("SELECT/**/1; SELECT '-- /* ; */' AS \"a;b\"; -- tail") == [
        "SELECT/**/1",
        " SELECT '-- /* ; */' AS \"a;b\"",
    ]


@pytest.mark.parametrize("tag", ["", "tag"])
@pytest.mark.parametrize("value", ["a/*b*/c", "a--b", "a; ' /* $other$ --\nb"])
def test_seed_splitting_preserves_dollar_quoted_values(tag, value):
    delimiter = f"${tag}$"
    db = Database.connect_in_memory()
    try:
        db.execute_script(
            "-- the table's value;\nCREATE TABLE seed_example AS SELECT "
            f"{delimiter}{value}{delimiter} AS value; /* tail's ; */ SELECT 2;"
        )
        assert db.query("SELECT value FROM seed_example") == [{"value": value}]
    finally:
        db.close()


@pytest.mark.parametrize("literal", ["$$a/*b*/c", "$tag$a--b$other$"])
def test_unclosed_seed_dollar_quote_refuses_before_any_execution(literal):
    db = Database.connect_in_memory()
    try:
        with pytest.raises(SemanticLayerError) as caught:
            db.execute_script(f"CREATE TABLE seed_example(value TEXT); SELECT {literal}")
        assert caught.value.code == "INVALID_CONFIG"
        assert caught.value.details == {"reason": "unterminated_sql_script"}
        assert (
            db.query(
                "SELECT table_name FROM information_schema.tables WHERE table_name = 'seed_example'"
            )
            == []
        )
    finally:
        db.close()


@pytest.mark.parametrize("value", ["/*x*/", "--x", "; /*x*/ --x"])
def test_seed_escape_strings_execute_with_data_intact(value):
    statement = rf"CREATE TABLE seed_example AS SELECT E'it\'s {value}' AS value"
    assert _split_sql_statements(statement) == [statement]
    db = Database.connect_in_memory()
    try:
        db.execute_script(statement + "; SELECT 2;")
        assert db.query("SELECT value FROM seed_example") == [{"value": f"it's {value}"}]
    finally:
        db.close()


@pytest.mark.parametrize(
    "fragment",
    ["/* unterminated", "/* outer /* inner */", "'unterminated", '"unterminated', r"E'x\'"],
)
def test_unclosed_seed_construct_refuses_before_any_execution(fragment):
    db = Database.connect_in_memory()
    try:
        with pytest.raises(SemanticLayerError) as caught:
            db.execute_script(f"CREATE TABLE seed_example(value TEXT); SELECT {fragment}")
        assert caught.value.code == "INVALID_CONFIG"
        assert caught.value.details == {"reason": "unterminated_sql_script"}
        assert (
            db.query(
                "SELECT table_name FROM information_schema.tables WHERE table_name = 'seed_example'"
            )
            == []
        )
    finally:
        db.close()


@pytest.mark.parametrize(
    "statement",
    [
        r" SELECT e'it\'s; /*x*/ --x' AS value",
        " SELECT 'it''s; /*x*/' AS value",
        ' SELECT 1 AS "a"";b"',
        " SELECT 1 AS account$tag$name",
        " SELECT $é$a; /*x*/ --x$é$ AS value",
    ],
)
def test_seed_statement_text_is_unchanged(statement):
    assert _split_sql_statements(statement + "; /* keep */ SELECT 2; -- tail") == [
        statement,
        " /* keep */ SELECT 2",
    ]


@pytest.mark.parametrize("engine", ["duckdb", "sqlite"])
@pytest.mark.parametrize(
    "tail",
    ["-- comment\rINSERT INTO seed_example VALUES (7);", "-- comment\rSELECT 'unterminated"],
)
def test_bare_carriage_return_refuses_before_any_execution(engine, tail):
    db = Database.connect(":memory:", engine=engine)
    try:
        with pytest.raises(SemanticLayerError) as caught:
            db.execute_script(f"CREATE TABLE seed_example(value INTEGER); {tail}")
        assert caught.value.code == "INVALID_CONFIG"
        assert caught.value.details["reason"] == "bare_carriage_return_sql_script"
        catalog = "information_schema.tables" if engine == "duckdb" else "sqlite_master"
        field = "table_name" if engine == "duckdb" else "name"
        assert db.query(f"SELECT {field} FROM {catalog} WHERE {field} = 'seed_example'") == []
    finally:
        db.close()


@pytest.mark.parametrize(
    "script", ["SELECT 1; -- tail\r", "SELECT 'a\rb';", "-- ok\r\nSELECT 1;\r"]
)
def test_splitter_refuses_bare_carriage_return_on_direct_calls(script):
    with pytest.raises(SemanticLayerError) as caught:
        _split_sql_statements(script)
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.details["reason"] == "bare_carriage_return_sql_script"


@pytest.mark.parametrize("seed_kind", ["sql_script", "csv_dir_duckdb"])
def test_seed_file_bare_carriage_return_refuses_with_filename(tmp_path, seed_kind):
    source = tmp_path / "seed.sql"
    source.write_bytes(
        b"CREATE TABLE seed_example(value INTEGER); -- comment\rINSERT INTO seed_example VALUES (7);"
    )
    target = tmp_path / "seed.duckdb"
    with pytest.raises(SemanticLayerError) as caught:
        if seed_kind == "sql_script":
            seed_db(str(target), str(source))
        else:
            load_csv_dir_to_duckdb(str(target), str(tmp_path), str(source))
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.details == {
        "reason": "bare_carriage_return_sql_script",
        "file": str(source),
    }
    assert str(source) in str(caught.value)
    assert not target.exists()


@pytest.mark.parametrize("seed_kind", ["sql_script", "csv_dir_duckdb"])
def test_crlf_seed_file_executes_every_statement(tmp_path, seed_kind):
    source = tmp_path / "seed.sql"
    source.write_bytes(
        b"CREATE TABLE seed_example(value INTEGER); -- comment\r\n"
        b"INSERT INTO seed_example VALUES (7); -- second\r\n"
        b"INSERT INTO seed_example VALUES (9); -- tail\r\n"
    )
    target = tmp_path / "seed.duckdb"
    if seed_kind == "sql_script":
        seed_db(str(target), str(source))
    else:
        load_csv_dir_to_duckdb(str(target), str(tmp_path), str(source))
    db = Database.connect(str(target))
    try:
        assert db.query("SELECT value FROM seed_example ORDER BY value") == [
            {"value": 7},
            {"value": 9},
        ]
    finally:
        db.close()


def test_rewrites_identifiers_to_backticks():
    sql = 'SELECT base.g1 AS "dimension.jaffle_product_type" FROM t ORDER BY "g1"'
    assert rewrite_double_quoted_identifiers(sql) == (
        "SELECT base.g1 AS `dimension.jaffle_product_type` FROM t ORDER BY `g1`"
    )


def test_leaves_single_quoted_literals_untouched():
    sql = 'SELECT \'a "quoted" word\' AS "alias", \'\' AS "empty" FROM t'
    assert rewrite_double_quoted_identifiers(sql) == (
        "SELECT 'a \"quoted\" word' AS `alias`, '' AS `empty` FROM t"
    )


def test_handles_escaped_quotes_inside_literals_and_identifiers():
    # '' inside a string stays; "" inside an identifier collapses to one ".
    sql = 'SELECT \'it\'\'s "fine"\' AS "odd""name" FROM t'
    assert rewrite_double_quoted_identifiers(sql) == (
        "SELECT 'it''s \"fine\"' AS `odd\"name` FROM t"
    )


def test_noop_without_double_quotes():
    sql = "SELECT a, 'b' FROM t WHERE c = 'd''e'"
    assert rewrite_double_quoted_identifiers(sql) == sql


# -- map_double_quoted_identifiers (the scanning core) -----------------------


def test_map_passes_unescaped_inner_text_and_emits_replacement_verbatim():
    sql = 'SELECT 1 AS "a""b", \'keep "this"\' AS note'
    seen: list[str] = []

    def upper(name: str) -> str:
        seen.append(name)
        return f"[{name.upper()}]"

    assert map_double_quoted_identifiers(sql, upper) == (
        'SELECT 1 AS [A"B], \'keep "this"\' AS note'
    )
    assert seen == ['a"b']


# -- float_nullif_divisions (shared ratio compat pass) ------------------------


def test_float_nullif_divisions_casts_with_the_given_type():
    sql = "SELECT a / NULLIF(b, 0) AS r FROM t"
    assert float_nullif_divisions(sql, cast_type="DOUBLE") == (
        "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) AS r FROM t"
    )
    assert float_nullif_divisions(sql, cast_type="DOUBLE PRECISION") == (
        "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE PRECISION) AS r FROM t"
    )


def test_float_nullif_divisions_recurses_and_skips_literals():
    sql = "SELECT a / NULLIF(b / NULLIF(c, 0), 0) AS r, ' / NULLIF(' AS note FROM t"
    assert float_nullif_divisions(sql, cast_type="DOUBLE") == (
        "SELECT a / CAST(NULLIF(b / CAST(NULLIF(c, 0) AS DOUBLE), 0) AS DOUBLE) AS r, "
        "' / NULLIF(' AS note FROM t"
    )


def test_float_nullif_divisions_leaves_unbalanced_tail_untouched():
    sql = "SELECT a / NULLIF(b, 0 FROM t"
    assert float_nullif_divisions(sql, cast_type="DOUBLE") == sql


# -- option_or_env (literal-or-*_env locator resolution) ----------------------


def test_option_or_env_prefers_the_literal(monkeypatch):
    monkeypatch.setenv("SR_TEST_HOST", "from-env")
    assert option_or_env({"host": "literal", "host_env": "SR_TEST_HOST"}, "host") == "literal"


def test_option_or_env_falls_back_to_env_and_records_missing(monkeypatch):
    monkeypatch.setenv("SR_TEST_HOST", "from-env")
    assert option_or_env({"host_env": "SR_TEST_HOST"}, "host") == "from-env"

    monkeypatch.delenv("SR_TEST_HOST", raising=False)
    missing: list[str] = []
    assert option_or_env({"host_env": "SR_TEST_HOST"}, "host", missing) == ""
    assert missing == ["SR_TEST_HOST"]

    # No literal and no *_env option: empty, and nothing recorded.
    missing = []
    assert option_or_env({}, "host", missing) == ""
    assert missing == []
