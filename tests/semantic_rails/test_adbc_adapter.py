"""Driver-free checks for Postgres dispatch, immutable binds and bounded batches."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
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


def _arrow_cursor(batch):
    pa = pytest.importorskip("pyarrow")

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


def _positional_rows(batch):
    """Rows as the correctness reference reads them: positional tuples."""
    from tests.integration.correctness.conftest import _rows

    cursor = _arrow_cursor(batch)
    adapter = SimpleNamespace(_connection=lambda: SimpleNamespace(cursor=lambda: cursor))
    return _rows(SimpleNamespace(_get_adapter=lambda: adapter), "SELECT 1")


def _arrow_rows(batch, positional):
    """Rows from the reference reader, or from the adapter capped at one row."""
    if positional:
        return [dict(zip(batch.schema.names, row, strict=True)) for row in _positional_rows(batch)]
    return AdbcAdapter._rows(_arrow_cursor(batch), {"max_rows": 1}, "UTC")


# Every unsupported kind is refused from the schema alone, whatever the batch holds,
# by both readers; the kinds that carry a canary hold it in their value.
@pytest.mark.parametrize(
    ("kind", "contents", "positional"),
    [
        ("numeric_list", "value", False),
        ("list", "null", True),
        ("large_list", "empty", False),
        ("fixed_list", "value", True),
        ("struct", "null", False),
        ("map", "empty", True),
        ("json", "value", False),
        ("unknown_extension", "value", True),
        ("invalid_numeric_storage", "null", False),
        ("large_binary", "value", False),
        ("fixed_binary_15", "empty", True),
        ("fixed_binary_16", "value", False),
        ("uuid_extension", "null", True),
        ("fixed_binary_17", "empty", False),
        ("opaque_uuid", "value", True),
        ("opaque_uuid_binary", "null", False),
        ("time32", "empty", True),
        ("time_ns", "value", False),
        ("duration", "null", True),
        ("date64", "empty", False),
        ("timestamp_ns", "value", True),
        ("dictionary", "value", True),
    ],
)
def test_unsupported_postgres_result_types_refuse_before_reading_values(kind, contents, positional):
    pa = pytest.importorskip("pyarrow")
    numeric = pa.opaque(pa.string(), "numeric", "PostgreSQL")
    data_type, value = {
        "numeric_list": (pa.list_(numeric), ["1.20", "123456789012345678.123456789", None]),
        "list": (pa.list_(pa.int64()), [1, None]),
        "large_list": (pa.large_list(numeric), ["1.20"]),
        "fixed_list": (pa.list_(numeric, 1), ["1.20"]),
        "struct": (pa.struct([("n", numeric)]), {"n": "1.20"}),
        "map": (pa.map_(pa.string(), numeric), [("n", "1.20")]),
        "json": (pa.json_(), '{"n":1,"secret":"row-canary"}'),
        "unknown_extension": (pa.opaque(pa.string(), "unknown", "PostgreSQL"), "row-canary"),
        "invalid_numeric_storage": (pa.opaque(pa.int64(), "numeric", "PostgreSQL"), 1),
        "large_binary": (pa.large_binary(), b"row-canary"),
        "fixed_binary_15": (pa.binary(15), b"x" * 15),
        "fixed_binary_16": (pa.binary(16), b"x" * 16),
        "uuid_extension": (pa.uuid(), b"x" * 16),
        "fixed_binary_17": (pa.binary(17), b"x" * 17),
        "opaque_uuid": (pa.opaque(pa.binary(16), "uuid", "PostgreSQL"), b"x" * 16),
        "opaque_uuid_binary": (pa.opaque(pa.binary(), "uuid", "PostgreSQL"), b"x" * 16),
        "time32": (pa.time32("s"), 1),
        "time_ns": (pa.time64("ns"), 1),
        "duration": (pa.duration("us"), 1),
        "date64": (pa.date64(), 0),
        "timestamp_ns": (pa.timestamp("ns"), 1),
        "dictionary": (pa.dictionary(pa.int8(), pa.string()), "row-canary"),
    }[kind]
    values = [value] if contents == "value" else [None] if contents == "null" else []
    # Build nested extension arrays from their storage layouts: PyArrow's
    # Python sequence builder does not construct extension children directly.
    storage_type = {
        "numeric_list": pa.list_(pa.string()),
        "large_list": pa.large_list(pa.string()),
        "fixed_list": pa.list_(pa.string(), 1),
        "struct": pa.struct([("n", pa.string())]),
        "map": pa.map_(pa.string(), pa.string()),
        "uuid_extension": pa.binary(16),
    }.get(kind, data_type)
    array = pa.array(values, type=storage_type).view(data_type)
    with pytest.raises(SemanticLayerError) as caught:
        _arrow_rows(pa.record_batch([array], names=["payload"]), positional)
    assert caught.value.code == "RESULT_TYPE_UNSUPPORTED"
    assert caught.value.details == {"column": "payload", "type": str(data_type)}
    assert "row-canary" not in str(caught.value)


SUPPORTED_SCALARS = [
    ("int8", -128, "(-128)::TINYINT"),
    ("int16", -32768, "(-32768)::SMALLINT"),
    ("int32", -2147483648, "(-2147483648)::INTEGER"),
    ("int64", 9007199254740993, "9007199254740993::BIGINT"),
    ("uint64", 18446744073709551615, "18446744073709551615::UBIGINT"),
    ("float32", 1.25, "1.25::FLOAT"),
    ("float64", 1.25, "1.25::DOUBLE"),
    ("string", "123.4500", "'123.4500'::VARCHAR"),
    ("bool_", True, "TRUE"),
    ("null", None, "NULL"),
    ("numeric", "123456789.4500", "123456789.4500::DECIMAL(20,4)"),
    ("decimal128", "123456789.4500", "123456789.4500::DECIMAL(20,4)"),
    ("decimal256", "123456789.4500", "123456789.4500::DECIMAL(20,4)"),
    ("date32", "2026-09-30", "DATE '2026-09-30'"),
    ("time", "12:34:56.123456", "TIME '12:34:56.123456'"),
    # Integer-backed times exercise the driver's raw representation at both ends of a day.
    ("time", 0, "TIME '00:00:00'"),
    ("time", 86_399_999_999, "TIME '23:59:59.999999'"),
    ("binary", b"\x00\xffrow-canary", r"'\x00\xFFrow-canary'::BLOB"),
    ("binary", b"", "''::BLOB"),
    ("binary", b"x" * 16, "'xxxxxxxxxxxxxxxx'::BLOB"),
    (
        "string",
        "12345678-1234-5678-9abc-def012345678",
        "'12345678-1234-5678-9abc-def012345678'::VARCHAR",
    ),
    ("timestamp", "2026-09-30T12:34:56.123456", "TIMESTAMP '2026-09-30 12:34:56.123456'"),
    (
        "timestamptz",
        "2026-09-30T07:04:56.123456+00:00",
        "TIMESTAMPTZ '2026-09-30 12:34:56.123456+05:30'",
    ),
    ("interval", (1, 2, 3123456000), "INTERVAL '1 month 2 days 3.123456 seconds'"),
    ("interval", (0, 1, 2000003000), "INTERVAL '1 day 2.000003 seconds'"),
    ("interval", (-1, 2, -3123456000), "INTERVAL '-1 month 2 days -3.123456 seconds'"),
    ("interval", (13, 0, 0), "INTERVAL '1 year 1 month'"),
    ("interval", (0, 0, -1000), "INTERVAL '-0.000001 seconds'"),
    ("interval", (0, 0, 0), "INTERVAL '0 seconds'"),
]


@pytest.mark.parametrize(
    ("type_name", "value", "literal", "positional"),
    [(*case, index % 2 == 1) for index, case in enumerate(SUPPORTED_SCALARS)],
)
def test_supported_postgres_scalars_encode_identically_to_duckdb(
    type_name, value, literal, positional
):
    pa = pytest.importorskip("pyarrow")
    if type_name == "numeric":
        data_type = pa.opaque(pa.string(), "numeric", "PostgreSQL")
    elif type_name.startswith("decimal"):
        data_type = getattr(pa, type_name)(20, 4)
        value = Decimal(value)
    elif type_name == "date32":
        data_type, value = pa.date32(), date.fromisoformat(value)
    elif type_name == "time":
        data_type = pa.time64("us")
        value = time.fromisoformat(value) if isinstance(value, str) else value
    elif type_name in ("timestamp", "timestamptz"):
        data_type = pa.timestamp("us", tz="UTC" if type_name == "timestamptz" else None)
        value = datetime.fromisoformat(value)
    elif type_name == "interval":
        data_type = pa.month_day_nano_interval()
    else:
        data_type = getattr(pa, type_name)()
    batch = pa.record_batch([pa.array([value, None], type=data_type)], names=["payload"])
    # The dict path also exercises bounded conversion; compare its first row.
    rows = _arrow_rows(batch, positional)
    python_type = {
        "numeric": Decimal,
        "decimal128": Decimal,
        "decimal256": Decimal,
        "time": time,
        "binary": bytes,
        "interval": timedelta,
    }.get(type_name)
    if python_type:
        assert type(rows[0]["payload"]) is python_type
    if python_type is Decimal:
        assert rows[0]["payload"].as_tuple().exponent == -4
    db = Database.connect(":memory:")
    try:
        if type_name == "timestamptz":
            reference_rows = (
                db.conn.execute(f"SELECT {literal} AS payload").to_arrow_table().to_pylist()
            )
        else:
            reference_rows = db.query(f"SELECT {literal} AS payload")
        reference = result_rows(reference_rows)
    finally:
        db.close()
    assert (
        json.dumps(result_rows(rows[:1]), allow_nan=False, sort_keys=True).encode()
        == json.dumps(reference, allow_nan=False, sort_keys=True).encode()
    )
    if positional:
        assert rows[1] == {"payload": None}
        assert result_rows(rows)["rows"][1] == {"payload": None}


@pytest.mark.parametrize(
    ("kind", "contents", "positional"),
    [
        ("time", "null", False),
        ("time", "empty", True),
        ("binary", "null", True),
        ("binary", "empty", False),
    ],
)
def test_supported_postgres_scalar_schemas_allow_null_and_empty_results(kind, contents, positional):
    pa = pytest.importorskip("pyarrow")
    data_type = {"time": pa.time64("us"), "binary": pa.binary()}[kind]
    values = [None] if contents == "null" else []
    batch = pa.record_batch([pa.array(values, type=data_type)], names=["payload"])
    assert _arrow_rows(batch, positional) == ([{"payload": None}] if contents == "null" else [])


# A time outside one day (24:00:00 must not collapse into the preceding midnight,
# even as the capped reader's truncation sentinel) and an interval with no exact
# timedelta refuse without echoing the value.
@pytest.mark.parametrize(
    ("value", "positional"),
    [
        (-1, True),
        (86_400_000_000, False),
        (86_400_000_001, True),
        ((0, 0, 1), False),
        ((0, 0, -1), True),
        ((2147483647, 0, 0), False),
    ],
)
def test_unrepresentable_postgres_values_refuse_without_values(value, positional):
    pa = pytest.importorskip("pyarrow")
    if isinstance(value, tuple):
        column = pa.array([value], type=pa.month_day_nano_interval())
        code, details = "RESULT_VALUE_UNSUPPORTED", {}
    else:
        column = pa.array([0, value], type=pa.time64("us"))
        code, details = "RESULT_TYPE_UNSUPPORTED", {"column": "payload", "type": "time64[us]"}
    with pytest.raises(SemanticLayerError) as caught:
        _arrow_rows(pa.record_batch([column], names=["payload"]), positional)
    assert (caught.value.code, caught.value.details) == (code, details)
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


MISMATCH = {"reason": "parameter_placeholder_mismatch"}


@pytest.mark.parametrize(
    ("sql", "slots", "values", "details"),
    [
        # Unfinalized, misaligned or quoted placeholders.
        ("SELECT ?", (SLOT,), ("canary",), MISMATCH),
        ("SELECT $2", (SLOT,), ("canary",), MISMATCH),
        ("SELECT $1, $1", (SLOT,), ("canary",), MISMATCH),
        ("SELECT '?'", (SLOT,), ("canary",), MISMATCH),
        ("SELECT $1, ?", (SLOT,), ("canary",), MISMATCH),
        # A JSON ? operator beside a bind stays refused.
        ("SELECT '{}'::jsonb ? $1", (SLOT,), ("a",), MISMATCH),
        # A placeholder without an authored slot.
        ("SELECT $1", (), (), MISMATCH),
        # Values that do not match their slots exactly.
        ("SELECT $1", (SLOT,), (), {"reason": "parameter_count_mismatch"}),
        ("SELECT $1", (SLOT,), (None,), {"reason": "missing_attribute", "attribute": "tenant"}),
        (
            "SELECT $1",
            (SLOT,),
            (True,),
            {"reason": "attribute_type_mismatch", "attribute": "tenant"},
        ),
        ("SELECT $1", (SLOT,), ("canary", "extra"), {"reason": "parameter_count_mismatch"}),
    ],
)
def test_bad_prepared_calls_deny_before_connect(sql, slots, values, details, monkeypatch):
    adapter = AdbcAdapter()
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("must deny before connection"))
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query_prepared(PreparedQuery(sql, parameters=slots), parameters=values)
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details == details
    assert "canary" not in str(caught.value)


@pytest.mark.parametrize("sql", ["SELECT ?, ?", "SELECT $1", "SELECT '?'", "SELECT ? + $2"])
def test_bad_finalization_fails_closed(sql):
    with pytest.raises(SemanticLayerError) as caught:
        finalize_parameters(PreparedQuery(sql, parameters=(SLOT,)), "postgres_native")
    assert caught.value.code == "POLICY_DENIED"


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
    [(0, 3, False, 2), (1, 1, True, 1), (3, 3, False, 2)],
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


@pytest.mark.parametrize(
    ("options", "limits", "timeout"),
    [
        # No override leaves the inherited timeout and zone untouched.
        (None, None, None),
        (None, {}, None),
        (None, {"time_zone": "UTC"}, None),
        # An override restores the exact prior session value.
        ({}, {"statement_timeout_ms": 250}, "250"),
        ({"statement_timeout_seconds": "1"}, None, "1000"),
    ],
)
def test_session_timeout_changes_only_for_an_override_and_is_restored(
    monkeypatch, options, limits, timeout
):
    adapter, cursor = _recording_adapter(monkeypatch, options)
    adapter.query("SELECT 1", limits=limits)
    settings = ("SELECT current_setting('TimeZone'), current_setting('statement_timeout')", None)
    set_timeout = "SELECT set_config('statement_timeout', $1, false)"
    assert list(zip(cursor.statements, cursor.parameters, strict=True)) == (
        [settings, ("SELECT 1", None)]
        if timeout is None
        else [settings, (set_timeout, (timeout,)), ("SELECT 1", None), (set_timeout, ("5s",))]
    )


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


@pytest.mark.parametrize("zone", ["GMT+5", "<+05>-05"])
def test_non_iana_session_zone_preserves_real_arrow_utc_timestamps(zone):
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


def test_unqualified_adbc_profile_is_refused():
    from semantic_rails.db_parts.adbc import POSTGRES_PROFILE

    with pytest.raises(SemanticLayerError) as caught:
        AdbcAdapter(profile=replace(POSTGRES_PROFILE, driver="untrusted.driver"))
    assert caught.value.code == "INVALID_CONFIG"


def test_exact_numeric_and_aware_timestamp_conversion():
    pa = pytest.importorskip("pyarrow")
    numeric = pa.opaque(pa.string(), "numeric", "PostgreSQL")
    batch = pa.record_batch(
        [
            pa.array(["123456789.4500"], type=numeric),
            pa.array(["123.4500"]),
            pa.array([None], type=numeric),
            pa.array(
                [datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
        ],
        names=["amount", "text", "missing", "instant"],
    )
    row = AdbcAdapter._rows(_arrow_cursor(batch), None, "Asia/Kolkata")[0]
    assert type(row["amount"]) is Decimal
    assert row["amount"] == Decimal("123456789.4500")
    assert row["amount"].as_tuple().exponent == -4
    assert type(row["text"]) is str
    assert row["missing"] is None
    assert row["instant"].microsecond == 123456
    assert row["instant"].utcoffset() == timedelta(hours=5, minutes=30)
    assert row["instant"].astimezone(UTC) == datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)


def test_unsupported_result_type_keeps_its_code_and_discards_the_session(monkeypatch):
    pa = pytest.importorskip("pyarrow")
    batch = pa.record_batch(
        [pa.array(['{"secret":"row-canary"}'], type=pa.json_())], names=["payload"]
    )
    adapter, cursor = _recording_adapter(monkeypatch)
    closed = []
    adapter._conn.close = lambda: closed.append(True)
    monkeypatch.setattr(cursor, "fetch_record_batch", _arrow_cursor(batch).fetch_record_batch)
    with pytest.raises(SemanticLayerError) as caught:
        adapter.query("SELECT 1")
    assert caught.value.code == "RESULT_TYPE_UNSUPPORTED"
    assert caught.value.details == {"column": "payload", "type": str(pa.json_())}
    assert "row-canary" not in str(caught.value)
    assert adapter._conn is None and closed == [True]


def test_positional_postgres_results_preserve_exact_numeric_types_and_duplicate_names():
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
    rows = _positional_rows(batch)
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
