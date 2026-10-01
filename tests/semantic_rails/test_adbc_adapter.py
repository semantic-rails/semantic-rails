"""Driver-free checks for Postgres dispatch, immutable binds and bounded batches."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from semantic_rails.db import Database, create_warehouse_adapter
from semantic_rails.db_parts.adbc import AdbcAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.result_values import result_rows
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_preparation import (
    ParameterSlot,
    PreparedQuery,
    finalize_parameters,
    postgres_parameter_tokens,
)

SLOT = ParameterSlot("tenant", "string")


def _interval_cursor(values):
    pa = pytest.importorskip("pyarrow")
    batch = pa.record_batch(
        [pa.array(values, type=pa.month_day_nano_interval())], names=["duration"]
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            assert sql == "SELECT 1"

        def fetch_record_batch(self):
            return pa.RecordBatchReader.from_batches(batch.schema, [batch])

    return Cursor()


@pytest.mark.parametrize("positional", [False, True])
@pytest.mark.parametrize(
    "value, literal",
    [
        ((0, 1, 2000003000), "1 day 2.000003 seconds"),
        ((1, 2, 3123456000), "1 month 2 days 3.123456 seconds"),
        ((-1, 2, -3123456000), "-1 month 2 days -3.123456 seconds"),
        ((13, 0, 0), "1 year 1 month"),
        ((0, 0, -1000), "-0.000001 seconds"),
        ((0, 0, 0), "0 seconds"),
    ],
)
def test_postgres_intervals_encode_identically_to_duckdb(value, literal, positional):
    import json

    from tests.integration.correctness.conftest import _rows

    cursor = _interval_cursor([value, None])
    if positional:
        adapter = SimpleNamespace(_connection=lambda: SimpleNamespace(cursor=lambda: cursor))
        rows = [
            {"duration": row[0]}
            for row in _rows(SimpleNamespace(_get_adapter=lambda: adapter), "SELECT 1")
        ]
    else:
        rows = AdbcAdapter._rows(cursor, None, "UTC")
    assert type(rows[0]["duration"]) is timedelta
    db = Database.connect_in_memory()
    try:
        reference = result_rows(
            db.query(f"SELECT INTERVAL '{literal}' AS duration UNION ALL SELECT NULL::INTERVAL")
        )
    finally:
        db.close()
    encoded = result_rows(rows)
    assert encoded["column_types"] == {"duration": {"type": "interval"}}
    assert encoded["rows"][1] == {"duration": None}
    assert (
        json.dumps(encoded, allow_nan=False, sort_keys=True).encode()
        == json.dumps(reference, allow_nan=False, sort_keys=True).encode()
    )


@pytest.mark.parametrize("value", [(0, 0, 1), (0, 0, -1), (2147483647, 0, 0)])
@pytest.mark.parametrize("positional", [False, True])
def test_unrepresentable_postgres_intervals_refuse_without_values(value, positional):
    from tests.integration.correctness.conftest import _rows

    cursor = _interval_cursor([value])
    with pytest.raises(SemanticLayerError) as caught:
        if positional:
            adapter = SimpleNamespace(_connection=lambda: SimpleNamespace(cursor=lambda: cursor))
            _rows(SimpleNamespace(_get_adapter=lambda: adapter), "SELECT 1")
        else:
            AdbcAdapter._rows(cursor, None, "UTC")
    assert caught.value.code == "RESULT_VALUE_UNSUPPORTED"
    assert caught.value.details == {}
    assert str(value) not in str(caught.value)


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


def test_nested_comments_hide_tokens_in_both_finalization_and_execution(monkeypatch):
    adapter, cursor = _recording_adapter(monkeypatch)
    prepared = PreparedQuery("SELECT ? /* outer /* inner */ $2 ? */", parameters=(SLOT,))
    final = finalize_parameters(prepared, "postgres_native")
    assert final.sql == "SELECT $1 /* outer /* inner */ $2 ? */"
    adapter.query_prepared(final, parameters=("tenant",))
    assert (final.sql, ("tenant",)) == (cursor.statements[-1], cursor.parameters[-1])
    assert adapter.query("SELECT /* outer /* inner */ $1 */ 1")


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
    [(0, 3, False, 2), (1, 1, True, 1), (3, 3, False, 2), (4, 3, False, 2)],
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
            return ("UTC", "5s")

        def fetch_record_batch(self):
            return reader

    adapter = AdbcAdapter()
    monkeypatch.setattr(adapter, "_connection", lambda: SimpleNamespace(cursor=Cursor))
    prepared = PreparedQuery("SELECT $1 AS physical", (("physical", "semantic"),), (SLOT,))
    assert adapter.query_prepared(prepared, parameters=("canary' OR true",)) == [{"semantic": 7}]
    assert (prepared.sql, ("canary' OR true",)) in sent
    assert all("canary" not in sql for sql, _ in sent)
    assert replace(prepared, parameters=()).sql == prepared.sql


def _recording_adapter(monkeypatch, options=None):
    from tests.semantic_rails.test_prepared_queries import CaptureCursor

    cursor = CaptureCursor("n")
    monkeypatch.setattr(cursor, "fetchone", lambda: ("UTC", "5s"))
    adapter = AdbcAdapter(options)
    adapter._conn = SimpleNamespace(cursor=lambda: cursor)
    return adapter, cursor


@pytest.mark.parametrize("identifier", ["account$1", "_account$1$2", "compteé$1", "账户$1"])
def test_postgres_identifier_suffix_is_not_a_bind_token(monkeypatch, identifier):
    sql = f"SELECT 1 AS {identifier}"
    assert postgres_parameter_tokens(sql) == []
    assert finalize_parameters(PreparedQuery(sql), "postgres_native").sql == sql
    adapter, cursor = _recording_adapter(monkeypatch)
    assert adapter.query(sql)
    assert cursor.statements[-1] == sql
    assert cursor.parameters[-1] is None


@pytest.mark.parametrize(
    "literal",
    [r"E'it\'s'", r"e'it\'s $2 ? /* --'", r"E'backslash\\'", "E'it''s'"],
)
@pytest.mark.parametrize("alias", ["other", "account$1"])
def test_escape_string_and_identifier_preserve_one_separate_bind(monkeypatch, literal, alias):
    sql = f"SELECT {literal} AS label, $1::TEXT AS tenant, 'x' AS {alias}"
    tokens = postgres_parameter_tokens(sql)
    assert [token[0] for token in tokens] == ["$1"]
    assert sql[tokens[0].start() : tokens[0].end()] == "$1"
    unfinalized = PreparedQuery(sql.replace("$1::TEXT", "?::TEXT"), parameters=(SLOT,))
    prepared = finalize_parameters(unfinalized, "postgres_native")
    assert prepared.sql == sql
    assert prepared.parameters == (SLOT,)
    adapter, cursor = _recording_adapter(monkeypatch)
    value = "tenant' OR true --"
    assert adapter.query_prepared(prepared, parameters=(value,))
    assert (cursor.statements[-1], cursor.parameters[-1]) == (sql, (value,))
    assert all(value not in statement for statement in cursor.statements)


@pytest.mark.parametrize("limits", [None, {}, {"time_zone": "UTC"}])
def test_no_overrides_leave_inherited_timeout_and_zone_untouched(monkeypatch, limits):
    adapter, cursor = _recording_adapter(monkeypatch)
    adapter.query("SELECT 1", limits=limits)
    assert cursor.statements == [
        "SELECT current_setting('TimeZone'), current_setting('statement_timeout')",
        "SELECT 1",
    ]


@pytest.mark.parametrize(
    ("options", "limits", "expected"),
    [
        ({}, {"statement_timeout_ms": 250}, "250"),
        ({"statement_timeout_seconds": "1"}, None, "1000"),
    ],
)
def test_timeout_override_restores_exact_prior_session_value(
    monkeypatch, options, limits, expected
):
    adapter, cursor = _recording_adapter(monkeypatch, options)
    adapter.query("SELECT 1", limits=limits)
    assert list(zip(cursor.statements, cursor.parameters, strict=True)) == [
        ("SELECT current_setting('TimeZone'), current_setting('statement_timeout')", None),
        ("SELECT set_config('statement_timeout', $1, false)", (expected,)),
        ("SELECT 1", None),
        ("SELECT set_config('statement_timeout', $1, false)", ("5s",)),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '{}'::jsonb ? 'a'",
        "SELECT '{}'::jsonb ?| ARRAY['a']",
        "SELECT '{}'::jsonb ?& ARRAY['a']",
        "SELECT /* outer /* inner */ ? */ 1",
    ],
)
def test_plain_sql_operators_and_nested_comments_reach_execution(monkeypatch, sql):
    adapter, cursor = _recording_adapter(monkeypatch)
    assert adapter.query(sql) == [{"n": 1.25}, {"n": 2.5}]
    assert cursor.statements[-1] == sql


def test_parameterized_json_operator_remains_denied_before_connect(monkeypatch):
    adapter = AdbcAdapter()
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query_prepared(
            PreparedQuery("SELECT '{}'::jsonb ? $1", parameters=(SLOT,)), parameters=("a",)
        )
    assert caught.value.details == {"reason": "parameter_placeholder_mismatch"}


@pytest.mark.parametrize("zone", ["GMT+5", "<+05>-05"])
def test_non_iana_session_zone_preserves_real_arrow_utc_timestamps(zone):
    from datetime import UTC, datetime

    pa = pytest.importorskip("pyarrow")

    instant = datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)
    batch = pa.record_batch({"instant": pa.array([instant], type=pa.timestamp("us", tz="UTC"))})
    reader = pa.RecordBatchReader.from_batches(batch.schema, [batch])
    row = AdbcAdapter._rows(SimpleNamespace(fetch_record_batch=lambda: reader), None, zone)[0]
    assert row["instant"] == instant
    assert row["instant"].utcoffset() == UTC.utcoffset(None)


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


@pytest.mark.parametrize("sql", ["SELECT $1"])
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


def test_positional_postgres_results_preserve_exact_numeric_types_and_duplicate_names():
    from decimal import Decimal

    from tests.integration.correctness.conftest import _rows

    pa = pytest.importorskip("pyarrow")
    numeric = pa.opaque(pa.string(), "numeric", "PostgreSQL")
    batch = pa.record_batch(
        [
            pa.array(["123456789.4500", None], type=numeric),
            pa.array(["70.00", "0.0000"], type=numeric),
            pa.array(["123.4500", "70.00"]),
        ],
        names=["coalesce", "coalesce", "text"],
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            assert sql == "SELECT 1"

        def fetch_record_batch(self):
            return pa.RecordBatchReader.from_batches(batch.schema, [batch])

        def fetchall(self):
            return [("123456789.4500", "70.00", "123.4500"), (None, "0.0000", "70.00")]

    adapter = SimpleNamespace(_connection=lambda: SimpleNamespace(cursor=Cursor))
    rows = _rows(SimpleNamespace(_get_adapter=lambda: adapter), "SELECT 1")
    assert rows == [
        (Decimal("123456789.4500"), Decimal("70.00"), "123.4500"),
        (None, Decimal("0.0000"), "70.00"),
    ]
    assert type(rows[0][0]) is Decimal and rows[0][0].as_tuple().exponent == -4
    assert type(rows[0][1]) is Decimal and rows[0][1].as_tuple().exponent == -2
    assert type(rows[0][2]) is str


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
            if (
                failure == "timeout_reset"
                and self.executed
                and "set_config('statement_timeout'" in sql
            ) or (failure == "zone_reset" and self.executed and "set_config('TimeZone'" in sql):
                raise RuntimeError("reset failure")

        def fetchone(self):
            return ("UTC", "5s")

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
        adapter.query("SELECT 1", limits={"statement_timeout_ms": 250, "time_zone": "Asia/Tokyo"})
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
