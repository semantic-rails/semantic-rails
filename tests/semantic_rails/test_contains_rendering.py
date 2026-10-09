"""`contains` is a case-insensitive literal match on every dialect family: `%`, `_` and `\\` in
the text are escaped in the LIKE pattern, and quotes stay inside the literal."""

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
# literals double them; BigQuery and ClickHouse escape LIKE with a backslash by default.
STANDARD = r"(LOWER(t.name) LIKE '%50\%\_off\\x''y%' ESCAPE '\')"
BACKSLASH = r"(LOWER(t.name) LIKE '%50\\%\\_off\\\\x''y%' ESCAPE '\\')"
BACKSLASH_DEFAULT_ESCAPE = r"(LOWER(t.name) LIKE '%50\\%\\_off\\\\x''y%')"
EXPECTED = {
    "duckdb": STANDARD,
    "motherduck": STANDARD,
    "ducklake": STANDARD,
    "postgres": STANDARD,
    "athena": STANDARD,
    "snowflake": BACKSLASH,
    "databricks": BACKSLASH,
    "bigquery": BACKSLASH_DEFAULT_ESCAPE,
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


@pytest.mark.parametrize("warehouse", sorted(EXPECTED))
def test_contains_renders_an_escaped_case_folded_pattern(warehouse):
    sql = _rendered(warehouse)
    assert " ".join(sql.split("WHERE", 1)[1].split()) == EXPECTED[warehouse]


def test_contains_matches_the_text_literally_on_duckdb():
    rows = ["50%_OFF\\x'y today", "50x_off\\x'y", "500 offx'y", "nothing"]
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE t (name VARCHAR)")
        connection.executemany("INSERT INTO t VALUES (?)", [(row,) for row in rows])
        found = [row[0] for row in connection.execute(_rendered("duckdb")).fetchall()]
    assert found == ["50%_OFF\\x'y today"]
