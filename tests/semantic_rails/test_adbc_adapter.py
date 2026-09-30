"""Driver-free checks for Postgres dispatch, immutable binds and bounded batches."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.adbc import AdbcAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_preparation import ParameterSlot, PreparedQuery, finalize_parameters

SLOT = ParameterSlot("tenant", "string")


def test_postgres_dispatch_uses_the_qualified_profile():
    package = PackageMeta(
        package_id="adbc_test",
        name="Arrow test",
        description="Postgres dispatch",
        warehouse="postgres",
        connection=ConnectionSpec(kind="postgres_native"),
    )
    adapter = create_warehouse_adapter(package)
    assert isinstance(adapter, AdbcAdapter)
    assert adapter.supports_parameters is True
    adapter.close()


def test_finalization_skips_literals_identifiers_comments_and_dollar_quotes():
    sql = """SELECT '?' AS "?", $$?$$, $tag$?$tag$ -- ?
FROM t WHERE tenant = ? /* ? */ AND active = ?"""
    prepared = PreparedQuery(sql, parameters=(SLOT, ParameterSlot("active", "boolean")))
    final = finalize_parameters(prepared, "postgres_native")
    assert final.sql == sql.replace("tenant = ?", "tenant = $1").replace(
        "active = ?", "active = $2"
    )
    assert final.parameters == prepared.parameters
    assert prepared.sql == sql
    assert finalize_parameters(prepared, "duckdb") is prepared


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
        finalize_parameters(PreparedQuery(sql, parameters=(SLOT,)), "postgres_native")
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

        def fetchone(self):
            return ("UTC",)

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


@pytest.mark.parametrize("sql", ["SELECT $1", "SELECT ?"])
def test_placeholders_without_authored_slots_deny_before_connect(sql, monkeypatch):
    adapter = AdbcAdapter()
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query_prepared(PreparedQuery(sql))
    assert caught.value.details == {"reason": "parameter_placeholder_mismatch"}


def test_unqualified_adbc_profile_is_refused():
    from semantic_rails.db_parts.adbc import POSTGRES_PROFILE

    with pytest.raises(SemanticLayerError) as caught:
        AdbcAdapter(profile=replace(POSTGRES_PROFILE, driver="untrusted.driver"))
    assert caught.value.code == "INVALID_CONFIG"


def test_exact_numeric_and_aware_timestamp_conversion():
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    numeric = SimpleNamespace(type_name="numeric", vendor_name="PostgreSQL")
    reader = Reader(
        [
            Batch(
                [
                    {
                        "amount": "123456789.4500",
                        "text": "123.4500",
                        "missing": None,
                        "instant": datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC),
                    }
                ]
            )
        ]
    )
    reader.schema = [SimpleNamespace(name=name, type=numeric) for name in ("amount", "missing")]
    row = AdbcAdapter._rows(
        SimpleNamespace(fetch_record_batch=lambda: reader), None, "Asia/Kolkata"
    )[0]
    assert type(row["amount"]) is Decimal
    assert row["amount"] == Decimal("123456789.4500")
    assert row["amount"].as_tuple().exponent == -4
    assert type(row["text"]) is str
    assert row["missing"] is None
    assert row["instant"].microsecond == 123456
    assert row["instant"].utcoffset() == timedelta(hours=5, minutes=30)
    assert row["instant"].astimezone(UTC) == datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)


@pytest.mark.parametrize("failure", ["execute", "fetch", "timeout_reset", "zone_reset"])
def test_failures_finish_watchdog_before_discarding_connection(failure, monkeypatch):
    events = []

    class Cursor:
        adbc_statement = SimpleNamespace(set_options=lambda **kwargs: None)
        executed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, parameters=None):
            if sql == "SELECT 1":
                self.executed = True
                if failure == "execute":
                    raise RuntimeError("execute failure")
            if (failure == "timeout_reset" and sql == "RESET statement_timeout") or (
                failure == "zone_reset" and self.executed and "set_config('TimeZone'" in sql
            ):
                raise RuntimeError("reset failure")

        def fetchone(self):
            return ("UTC",)

        def fetch_record_batch(self):
            if failure == "fetch":
                raise RuntimeError("fetch failure")
            return Reader([Batch([{"n": 1}])])

    class Timer:
        def __init__(self, delay, callback):
            assert delay == 0.25

        def start(self):
            events.append("started")

        def cancel(self):
            events.append("cancelled")

        def join(self):
            events.append("joined")

    monkeypatch.setattr("semantic_rails.db_parts.adbc.threading.Timer", Timer)
    adapter = AdbcAdapter()
    adapter._conn = SimpleNamespace(cursor=Cursor, close=lambda: events.append("closed"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query("SELECT 1", limits={"statement_timeout_ms": 250})
    assert caught.value.code == "QUERY_EXECUTION_ERROR"
    assert events == ["started", "cancelled", "joined", "closed"]
    assert adapter._conn is None


def test_postgres_fixture_loader_ingests_batches_on_the_adapter_connection(monkeypatch, tmp_path):
    import sys
    import threading
    import types

    from tests.integration.fixture import FixtureColumn, FixtureTable, JaffleFixture
    from tests.integration.loaders.postgres import PostgresFixtureLoader

    batches = [object(), object()]
    calls = []
    arrow = types.ModuleType("pyarrow")
    parquet = types.ModuleType("pyarrow.parquet")

    class ParquetFile:
        def __init__(self, path):
            assert path == tmp_path / "fact.parquet"

        def iter_batches(self, batch_size):
            assert batch_size == 5000
            return iter(batches)

    parquet.ParquetFile = ParquetFile
    arrow.parquet = parquet
    monkeypatch.setitem(sys.modules, "pyarrow", arrow)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", parquet)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def adbc_ingest(self, name, data, *, mode, db_schema_name):
            calls.append((name, data, mode, db_schema_name))

    adapter = SimpleNamespace(
        _lock=threading.RLock(),
        _connection=lambda: SimpleNamespace(cursor=Cursor),
        options={"schema": "analytics"},
        query=lambda sql: calls.append(sql),
    )
    table = FixtureTable("fact", (FixtureColumn("n", "BIGINT", "integer"),), 2)
    fixture = JaffleFixture(tmp_path, "fingerprint", (table,))
    PostgresFixtureLoader(adapter).load_table(fixture, table)
    assert calls == [
        "DROP TABLE IF EXISTS fact",
        "CREATE TABLE fact (n BIGINT)",
        *(("fact", batch, "append", "analytics") for batch in batches),
    ]
