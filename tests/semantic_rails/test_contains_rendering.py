"""The text words (`contains`, `starts_with`, `ends_with` and their `not_` forms) are
case-insensitive literal matches on every dialect family: `%`, `_` and `\\` in the text are
escaped in the LIKE pattern, and quotes stay inside the literal."""

import duckdb
import pytest

from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.renderer import render_select_for_profile
from semantic_rails.sql_ast import (
    SqlField,
    SqlIdentifier,
    SqlSelect,
    SqlTableRef,
    build_filter_condition,
)

TEXT = "50%_Off\\x'y"
# Standard-conforming literals keep backslashes as they are and need ESCAPE; backslash-escaping
# literals double them, and BigQuery and Spark also escape the quote with one (they read ''
# as two literals); BigQuery and ClickHouse escape LIKE with a backslash by default.
STANDARD = r"(LOWER(t.name) LIKE '%50\%\_off\\x''y%' ESCAPE '\')"
BACKSLASH = r"(LOWER(t.name) LIKE '%50\\%\\_off\\\\x''y%' ESCAPE '\\')"
BACKSLASH_QUOTE = r"(LOWER(t.name) LIKE '%50\\%\\_off\\\\x\'y%' ESCAPE '\\')"
BACKSLASH_DEFAULT_ESCAPE = r"(LOWER(t.name) LIKE '%50\\%\\_off\\\\x''y%')"
BACKSLASH_QUOTE_DEFAULT_ESCAPE = r"(LOWER(t.name) LIKE '%50\\%\\_off\\\\x\'y%')"
EXPECTED = {
    "duckdb": STANDARD,
    "motherduck": STANDARD,
    "ducklake": STANDARD,
    "postgres": STANDARD,
    "athena": STANDARD,
    "snowflake": BACKSLASH,
    "databricks": BACKSLASH_QUOTE,
    "bigquery": BACKSLASH_QUOTE_DEFAULT_ESCAPE,
    "clickhouse": BACKSLASH_DEFAULT_ESCAPE,
}


def _rendered(warehouse: str, text: str = TEXT) -> str:
    condition = build_filter_condition(SqlIdentifier(["t", "name"]), "contains", text)
    select = SqlSelect(
        select=[SqlField(SqlIdentifier(["t", "name"]), "name")],
        from_table=SqlTableRef("t"),
        where=[condition],
    )
    return render_select_for_profile(select, dialect=dialect_for_warehouse(warehouse))


def test_contains_renders_an_escaped_case_folded_pattern_that_matches_literally():
    for warehouse, expected in EXPECTED.items():
        sql = _rendered(warehouse)
        assert " ".join(sql.split("WHERE", 1)[1].split()) == expected, warehouse
    rows = ["50%_OFF\\x'y today", "50x_off\\x'y", "500 offx'y", "nothing"]
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE t (name VARCHAR)")
        connection.executemany("INSERT INTO t VALUES (?)", [(row,) for row in rows])
        found = [row[0] for row in connection.execute(_rendered("duckdb")).fetchall()]
    assert found == ["50%_OFF\\x'y today"]


# The other text words take the same escaping and case folding: starts_with drops the leading
# %, ends_with the trailing one, and each not_ form is NOT LIKE that keeps unknown rows, as `not`.
SHAPES = {
    "contains": lambda sql: sql,
    "starts_with": lambda sql: sql.replace("LIKE '%", "LIKE '"),
    "ends_with": lambda sql: sql.replace("y%'", "y'"),
}


def _expected(word: str, warehouse: str) -> str:
    sql = SHAPES[word.removeprefix("not_")](EXPECTED[warehouse])
    if word.startswith("not_"):
        return f"{sql.replace(' LIKE ', ' NOT LIKE ')} OR (t.name IS NULL)"
    return sql


@pytest.mark.parametrize("word", [*SHAPES, *(f"not_{word}" for word in SHAPES)])
def test_each_text_word_renders_its_pattern_on_every_dialect(word):
    for warehouse in EXPECTED:
        condition = build_filter_condition(SqlIdentifier(["t", "name"]), word, TEXT)
        select = SqlSelect(
            select=[SqlField(SqlIdentifier(["t", "name"]), "name")],
            from_table=SqlTableRef("t"),
            where=[condition],
        )
        sql = render_select_for_profile(select, dialect=dialect_for_warehouse(warehouse))
        assert " ".join(sql.split("WHERE", 1)[1].split()) == _expected(word, warehouse), (
            word,
            warehouse,
        )


# Each word against rows that start with, end with, hold, or only wildcard-match the text.
ROWS = ["50%_OFF\\x'y today", "today 50%_off\\x'Y", "50x_off\\x'y", "500 offx'y", None]
MATCHES = {
    "contains": [0, 1],
    "starts_with": [0],
    "ends_with": [1],
    "not_contains": [2, 3, 4],
    "not_starts_with": [1, 2, 3, 4],
    "not_ends_with": [0, 2, 3, 4],
}


@pytest.mark.parametrize(("word", "expected"), MATCHES.items())
def test_each_text_word_matches_literally_ignoring_case_and_its_not_form_keeps_unknowns(
    word, expected
):
    condition = build_filter_condition(SqlIdentifier(["t", "name"]), word, TEXT)
    select = SqlSelect(
        select=[SqlField(SqlIdentifier(["t", "id"]), "id")],
        from_table=SqlTableRef("t"),
        where=[condition],
    )
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        connection.executemany("INSERT INTO t VALUES (?, ?)", list(enumerate(ROWS)))
        sql = render_select_for_profile(select, dialect=dialect_for_warehouse("duckdb"))
        found = sorted(row[0] for row in connection.execute(sql).fetchall())
    assert found == expected
