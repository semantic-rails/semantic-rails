"""A first or last value reads the rows whose value and order key are both known, on every
warehouse, as DuckDB's ``arg_min`` and ``arg_max`` (the reference) do.

Each warehouse's own NULL rules differ: Postgres's ``ARRAY_AGG`` keeps NULL values and sorts a
NULL key first when descending, BigQuery sorts a NULL key first when ascending, and Snowflake,
Databricks and Trino return a NULL value at the edge. The live Postgres check is in
``tests/integration/correctness/test_first_last_values.py``.
"""

from __future__ import annotations

import duckdb
import pytest

from semantic_rails.dialects import dialect_for_warehouse, supported_warehouses
from semantic_rails.renderer import render_expr, use_dialect
from semantic_rails.sql_ast import SqlIdentifier

VALUE, ORDER = SqlIdentifier(parts=["x"]), SqlIdentifier(parts=["o"])
KNOWN = "FILTER (WHERE x IS NOT NULL AND o IS NOT NULL)"
NO_KEY = "CASE WHEN (o IS NULL) THEN NULL ELSE x END"
NO_VALUE = "CASE WHEN (x IS NULL) THEN NULL ELSE o END"
FORMS = {
    "duckdb": ("arg_min(x, o)", "arg_max(x, o)"),
    "motherduck": ("arg_min(x, o)", "arg_max(x, o)"),
    "ducklake": ("arg_min(x, o)", "arg_max(x, o)"),
    "postgres": (
        f"(ARRAY_AGG(x ORDER BY o ASC) {KNOWN})[1]",
        f"(ARRAY_AGG(x ORDER BY o DESC) {KNOWN})[1]",
    ),
    "bigquery": (
        f"(ARRAY_AGG({NO_KEY} IGNORE NULLS ORDER BY o ASC LIMIT 1))[SAFE_OFFSET(0)]",
        f"(ARRAY_AGG({NO_KEY} IGNORE NULLS ORDER BY o DESC LIMIT 1))[SAFE_OFFSET(0)]",
    ),
    "snowflake": (f"MIN_BY(x, {NO_VALUE})", f"MAX_BY(x, {NO_VALUE})"),
    "databricks": (f"MIN_BY(x, o) {KNOWN}", f"MAX_BY(x, o) {KNOWN}"),
    "athena": (f"MIN_BY(x, o) {KNOWN}", f"MAX_BY(x, o) {KNOWN}"),
    # argMin/argMax skip a NULL value and a NULL key themselves.
    "clickhouse": ("argMin(x, o)", "argMax(x, o)"),
}
# The earliest row has no value and two rows no key: arg_min reads 5, arg_max 7.
ROWS = "(NULL, 1), (5, 2), (7, 3), (9, NULL), (NULL, 4), (NULL, NULL)"


def _forms(warehouse: str) -> tuple[str, str]:
    dialect = dialect_for_warehouse(warehouse)
    with use_dialect(dialect):
        first = render_expr(dialect.first_value(VALUE, ORDER))
        return first, render_expr(dialect.last_value(VALUE, ORDER))


@pytest.mark.parametrize("warehouse", sorted(supported_warehouses()))
def test_every_warehouse_reads_only_rows_with_a_value_and_a_key(warehouse):
    assert sorted(FORMS) == sorted(supported_warehouses())
    assert _forms(warehouse) == FORMS[warehouse]


@pytest.mark.parametrize("warehouse", ["postgres", "snowflake", "databricks", "athena"])
def test_the_forms_duckdb_can_run_give_the_reference_answer(warehouse):
    with duckdb.connect() as connection:
        connection.execute(f"CREATE TABLE t AS SELECT * FROM (VALUES {ROWS}) AS v(x, o)")
        reference = connection.execute("SELECT arg_min(x, o), arg_max(x, o) FROM t").fetchall()
        first, last = _forms(warehouse)
        assert connection.execute(f"SELECT {first}, {last} FROM t").fetchall() == reference
    assert reference == [(5, 7)]
