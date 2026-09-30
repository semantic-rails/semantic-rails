"""Driver-free checks for opt-in dispatch, immutable binds and bounded batches."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.adbc import AdbcAdapter
from semantic_rails.db_parts.postgres import PostgresAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_preparation import ParameterSlot, PreparedQuery, finalize_parameters

SLOT = ParameterSlot("tenant", "string")


def test_dispatch_is_opt_in():
    for kind, cls in (("postgres_native", PostgresAdapter), ("postgres_adbc", AdbcAdapter)):
        package = PackageMeta(
            package_id="adbc_test",
            name="Arrow test",
            description="Opt-in dispatch",
            warehouse="postgres",
            connection=ConnectionSpec(kind=kind),
        )
        adapter = create_warehouse_adapter(package)
        assert isinstance(adapter, cls)
        adapter.close()


def test_finalization_skips_literals_identifiers_comments_and_dollar_quotes():
    sql = """SELECT '?' AS "?", $$?$$, $tag$?$tag$ -- ?
FROM t WHERE tenant = ? /* ? */ AND active = ?"""
    prepared = PreparedQuery(sql, parameters=(SLOT, ParameterSlot("active", "boolean")))
    final = finalize_parameters(prepared, "postgres_adbc")
    assert final.sql == sql.replace("tenant = ?", "tenant = $1").replace(
        "active = ?", "active = $2"
    )
    assert final.parameters == prepared.parameters
    assert prepared.sql == sql
    assert finalize_parameters(prepared, "postgres_native") is prepared


@pytest.mark.parametrize(
    "sql", ["SELECT ?", "SELECT $2", "SELECT $1, $1", "SELECT '?'", "SELECT $1, ?"]
)
def test_unfinalized_or_misaligned_prepared_calls_deny_before_connect(sql, monkeypatch):
    adapter = AdbcAdapter()
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query_prepared(PreparedQuery(sql, parameters=(SLOT,)), parameters=("canary",))
    assert caught.value.details == {"reason": "parameter_placeholder_mismatch"}


@pytest.mark.parametrize("sql", ["SELECT ?, ?", "SELECT $1", "SELECT '?'", "SELECT ? + $2"])
def test_bad_finalization_fails_closed(sql):
    with pytest.raises(SemanticLayerError) as caught:
        finalize_parameters(PreparedQuery(sql, parameters=(SLOT,)), "postgres_adbc")
    assert caught.value.code == "POLICY_DENIED"


@pytest.mark.parametrize("values", [(), (None,), (True,), ("canary", "extra")])
def test_value_checks_precede_connection(values, monkeypatch):
    adapter = AdbcAdapter()
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query_prepared(PreparedQuery("SELECT $1", parameters=(SLOT,)), parameters=values)
    assert caught.value.code == "POLICY_DENIED"
    assert "canary" not in str(caught.value.details)


class Batch:
    def __init__(self, rows):
        self.rows = rows

    def slice(self, start, length):
        return Batch(self.rows[start : start + length])

    def to_pylist(self):
        return [dict(row) for row in self.rows]


class Reader:
    schema = ()

    def __init__(self, batches):
        self.batches = batches
        self.read = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        for batch in self.batches:
            self.read += 1
            yield batch


@pytest.mark.parametrize(
    ("cap", "count", "truncated", "reads"),
    [(0, 0, True, 1), (1, 1, True, 1), (3, 3, False, 2), (4, 3, False, 2)],
)
def test_bounded_fetch_reads_only_enough_batches(cap, count, truncated, reads):
    reader = Reader([Batch([{"n": 1}, {"n": 2}]), Batch([{"n": 3}])])
    rows = AdbcAdapter._rows(
        SimpleNamespace(fetch_record_batch=lambda: reader), {"max_rows": cap}, "UTC"
    )
    assert len(rows) == count
    assert rows.truncated is truncated
    assert reader.read == reads


def test_prepared_sql_and_values_reach_driver_separately(monkeypatch):
    sent = []
    reader = Reader([Batch([{"physical": 7}])])

    class Cursor:
        adbc_statement = SimpleNamespace(set_options=lambda **kwargs: None)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, parameters=None):
            sent.append((sql, parameters))

        def fetch_record_batch(self):
            return reader

    adapter = AdbcAdapter()
    monkeypatch.setattr(adapter, "_connection", lambda: SimpleNamespace(cursor=Cursor))
    prepared = PreparedQuery("SELECT $1 AS physical", (("physical", "semantic"),), (SLOT,))
    assert adapter.query_prepared(prepared, parameters=("canary' OR true",)) == [{"semantic": 7}]
    assert (prepared.sql, ("canary' OR true",)) in sent
    assert all("canary" not in sql for sql, _ in sent)
    assert replace(prepared, parameters=()).sql == prepared.sql


def test_query_failure_discards_session_and_redacts_driver_text(monkeypatch):
    sent = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, parameters=None):
            raise RuntimeError("driver-secret-canary")

    adapter = AdbcAdapter()
    adapter._conn = SimpleNamespace(cursor=Cursor, close=lambda: sent.append("closed"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query("SELECT 'sql-canary'")
    assert adapter._conn is None
    assert sent == ["closed"]
    assert caught.value.code == "QUERY_EXECUTION_ERROR"
    assert "driver-secret-canary" not in str(caught.value)
    assert "sql-canary" not in str(caught.value.details)
