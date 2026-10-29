"""The text words (`contains`, `starts_with`, `ends_with` and their `not_` forms) are
case-insensitive literal matches on every dialect family: `%`, `_` and `\\` in the text are
escaped in the LIKE pattern, and quotes stay inside the literal."""

import duckdb
import pytest

from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.renderer import render_select_for_profile
from semantic_rails.sql_ast import (
    SqlField,
    SqlIdentifier,
    SqlSelect,
    SqlTableRef,
    build_filter_condition,
)

TEXT = "50%_Off\\x'y"
# Both sides fold through one per-dialect function, so a column and its own text always fold
# alike: a column's own collation is set aside first (Postgres folds under the ICU root, and
# Snowflake, BigQuery and Databricks drop it), and ClickHouse reads a FixedString as String
# without its zero padding. Standard-conforming literals keep backslashes as they are and need
# ESCAPE; backslash-escaping literals double them, and BigQuery and Spark also escape the quote
# with one (they read '' as two literals); BigQuery and ClickHouse escape LIKE with a backslash
# by default.
STANDARD = r"(LOWER(t.name) LIKE LOWER('%50\%\_Off\\x''y%') ESCAPE '\')"
COLLATED = 'AS TEXT) COLLATE "und-x-icu")'
POSTGRES = (
    r"(LOWER(CAST(t.name "
    + COLLATED
    + r" LIKE LOWER(CAST('%50\%\_Off\\x''y%' "
    + COLLATED
    + r" ESCAPE '\')"
)
BACKSLASH = (
    r"(LOWER(COLLATE(t.name, '')) LIKE LOWER(COLLATE('%50\\%\\_Off\\\\x''y%', '')) ESCAPE '\\')"
)
BACKSLASH_QUOTE = (
    r"(LOWER((t.name) COLLATE UTF8_BINARY) LIKE LOWER(('%50\\%\\_Off\\\\x\'y%') COLLATE "
    r"UTF8_BINARY) ESCAPE '\\')"
)
BACKSLASH_QUOTE_DEFAULT_ESCAPE = (
    r"(LOWER(COLLATE(t.name, '')) LIKE LOWER(COLLATE('%50\\%\\_Off\\\\x\'y%', '')))"
)


def _clickhouse_fold(value: str) -> str:
    return (
        f"lowerUTF8(if(position(toTypeName({value}), 'FixedString') > 0, "
        f"toStringCutToZero(toString({value})), toString({value})))"
    )


UTF8_DEFAULT_ESCAPE = (
    "(" + _clickhouse_fold("t.name") + " LIKE " + _clickhouse_fold(r"'%50\\%\\_Off\\\\x''y%'") + ")"
)
EXPECTED = {
    "duckdb": STANDARD,
    "motherduck": STANDARD,
    "ducklake": STANDARD,
    "postgres": POSTGRES,
    "athena": STANDARD,
    "snowflake": BACKSLASH,
    "databricks": BACKSLASH_QUOTE,
    "bigquery": BACKSLASH_QUOTE_DEFAULT_ESCAPE,
    "clickhouse": UTF8_DEFAULT_ESCAPE,
}


def _rendered(warehouse: str, word: str, text: str = TEXT) -> str:
    condition = build_filter_condition(SqlIdentifier(["t", "name"]), word, text)
    select = SqlSelect(
        select=[SqlField(SqlIdentifier(["t", "name"]), "name")],
        from_table=SqlTableRef("t"),
        where=[condition],
    )
    return render_select_for_profile(select, dialect=dialect_for_warehouse(warehouse))


# The other text words take the same escaping and case folding: starts_with drops the leading
# %, ends_with the trailing one, and each not_ form is NOT LIKE that keeps unknown rows, as `not`.
SHAPES = {
    "contains": lambda sql: sql,
    "starts_with": lambda sql: sql.replace("'%50", "'50"),
    "ends_with": lambda sql: sql.replace("y%'", "y'"),
}


def _expected(word: str, warehouse: str) -> str:
    sql = SHAPES[word.removeprefix("not_")](EXPECTED[warehouse])
    if word.startswith("not_"):
        return f"({sql.replace(' LIKE ', ' NOT LIKE ')} OR (t.name IS NULL))"
    return sql


@pytest.mark.parametrize("word", [*SHAPES, *(f"not_{word}" for word in SHAPES)])
def test_each_text_word_renders_its_pattern_on_every_dialect(word):
    for warehouse in EXPECTED:
        sql = _rendered(warehouse, word)
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


# Each row folds alike on both sides: it meets every word with its own text, and accented
# capitals fold the same way in the column and in the text.
UNICODE = ["Émile", "ÉMILE", "émile", "ΟΣΑ", "Straße", "STRASSE", "İSTANBUL", "istanbul"]
# Letters whose lowercase depends on the next letter or changes length on some warehouse.
TRICKY = ["Σ", "σ", "ς", "ß", "ẞ", "İ", "ı", "K", "Å", "Ω", "̇"]


def _matching(connection, word: str, text: str) -> list[str]:
    sql = _rendered("duckdb", word, text)
    return sorted(row[0] for row in connection.execute(sql).fetchall())


@pytest.fixture
def unicode_names():
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE t (name VARCHAR)")
        connection.executemany("INSERT INTO t VALUES (?)", [(name,) for name in UNICODE])
        yield connection


def test_every_row_meets_each_text_word_with_its_own_text(unicode_names):
    for name in ["Émile", "ÉMILE", "émile", "STRASSE", "istanbul"]:
        for word in SHAPES:
            assert name in _matching(unicode_names, word, name), (word, name)
            assert name not in _matching(unicode_names, f"not_{word}", name), (word, name)


def test_accented_capitals_fold_alike_in_the_column_and_the_text(unicode_names):
    accented = ["ÉMILE", "Émile", "émile"]
    assert _matching(unicode_names, "starts_with", "é") == accented
    assert _matching(unicode_names, "starts_with", "É") == accented
    assert _matching(unicode_names, "ends_with", "MILE") == accented
    others = sorted(set(UNICODE) - set(accented))
    assert _matching(unicode_names, "not_starts_with", "É") == others
    # Text with no tricky letter matches a value that holds one away from it.
    assert _matching(unicode_names, "starts_with", "ο") == ["ΟΣΑ"]
    assert _matching(unicode_names, "contains", "SS") == ["STRASSE"]


@pytest.mark.parametrize("letter", TRICKY)
@pytest.mark.parametrize("word", [*SHAPES, *(f"not_{word}" for word in SHAPES)])
def test_text_with_a_letter_whose_lowercase_is_unstable_is_refused(word, letter):
    with pytest.raises(SemanticLayerError) as caught:
        build_filter_condition(SqlIdentifier(["t", "name"]), word, f"Ο{letter}A", path="where[0]")
    assert caught.value.details["path"] == "where[0].value"
    hint = caught.value.details["recovery_hints"][0]["message"]
    assert '"op": "is"' in hint and "valid-values" in hint


# An id reads through the dialect's cast to text before both sides fold; Postgres and ClickHouse
# fold any type as text already, so their SQL is the text column's.
ID_TEXT = "e2c0"
ID_STANDARD = r"(LOWER(CAST(t.id AS VARCHAR)) LIKE LOWER('%e2c0%') ESCAPE '\')"
ID_EXPECTED = {
    "duckdb": ID_STANDARD,
    "motherduck": ID_STANDARD,
    "ducklake": ID_STANDARD,
    "athena": ID_STANDARD,
    "postgres": (
        r'(LOWER(CAST(t.id AS TEXT) COLLATE "und-x-icu") LIKE '
        r"""LOWER(CAST('%e2c0%' AS TEXT) COLLATE "und-x-icu") ESCAPE '\')"""
    ),
    "snowflake": (
        r"(LOWER(COLLATE(CAST(t.id AS VARCHAR), '')) LIKE LOWER(COLLATE('%e2c0%', '')) "
        r"ESCAPE '\\')"
    ),
    "databricks": (
        r"(LOWER((CAST(t.id AS STRING)) COLLATE UTF8_BINARY) LIKE "
        r"LOWER(('%e2c0%') COLLATE UTF8_BINARY) ESCAPE '\\')"
    ),
    "bigquery": r"(LOWER(COLLATE(CAST(t.id AS STRING), '')) LIKE LOWER(COLLATE('%e2c0%', '')))",
    "clickhouse": "(" + _clickhouse_fold("t.id") + " LIKE " + _clickhouse_fold("'%e2c0%'") + ")",
}


def _id_where(warehouse: str, word: str, value: object) -> str:
    column = SqlIdentifier(["t", "id"])
    select = SqlSelect(
        select=[SqlField(column, "id")],
        from_table=SqlTableRef("t"),
        where=[build_filter_condition(column, word, value, as_text=True)],
    )
    sql = render_select_for_profile(select, dialect=dialect_for_warehouse(warehouse))
    return " ".join(sql.split("WHERE", 1)[1].split())


def test_an_id_reads_through_each_dialects_cast_to_text():
    assert set(ID_EXPECTED) == set(EXPECTED)
    for warehouse, expected in ID_EXPECTED.items():
        assert _id_where(warehouse, "contains", ID_TEXT) == expected, warehouse
        # A not_ form keeps the rows with no id, read as stored; is compares the id as stored.
        assert _id_where(warehouse, "not_contains", ID_TEXT).endswith(" OR (t.id IS NULL))")
        assert _id_where(warehouse, "is", 203) == "t.id = 203", warehouse


# Integer and UUID ids, as DuckDB stores them: each word reads the id's canonical text.
IDS = [
    (3, "e2c0be24-560d-78c5-e599-c2a9c9d0bbd2"),
    (103, None),
    (203, "00000000-0000-4000-8000-0000000000e2"),
    (None, None),
]
ID_MATCHES = [
    ("id", "ends_with", "03", [103, 203]),
    ("id", "starts_with", "3", [3]),
    ("id", "not_contains", "0", [3, None]),
    ("u", "starts_with", "E2C0", [3]),
    ("u", "contains", "-560D-", [3]),
    ("u", "ends_with", "E2", [203]),
    ("u", "not_starts_with", "e2c0", [103, 203, None]),
]


@pytest.mark.parametrize(("column", "word", "text", "expected"), ID_MATCHES)
def test_each_text_word_matches_an_id_by_its_canonical_text(column, word, text, expected):
    condition = build_filter_condition(SqlIdentifier(["t", column]), word, text, as_text=True)
    select = SqlSelect(
        select=[SqlField(SqlIdentifier(["t", "id"]), "id")],
        from_table=SqlTableRef("t"),
        where=[condition],
    )
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE t (id INTEGER, u UUID)")
        connection.executemany("INSERT INTO t VALUES (?, ?)", IDS)
        sql = render_select_for_profile(select, dialect=dialect_for_warehouse("duckdb"))
        found = [row[0] for row in connection.execute(sql).fetchall()]
    assert sorted(found, key=lambda number: (number is None, number)) == expected
