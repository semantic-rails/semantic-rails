from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import duckdb
import pytest

from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.cli.common import _print_json
from semantic_rails.cli.reports import ask_report
from semantic_rails.config_validation import PackageReference
from semantic_rails.db_parts.base import QueryRows
from semantic_rails.db_parts.snowflake import _extract_snowflake_json_rows
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text
from semantic_rails.result_values import result_rows
from tests.integration.harness import assert_column_types_match, assert_rows_match, normalize_rows

QUERY = {
    "version": 1,
    "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
    "verbosity": "minimal",
}


@pytest.mark.parametrize(
    ("value", "expected", "metadata"),
    [
        (Decimal("0.1"), 0.1, {"type": "decimal"}),
        (Decimal("1.2500"), 1.25, {"type": "decimal"}),
        (Decimal("42.0"), 42, {"type": "decimal"}),
        (42.0, 42, {"type": "decimal"}),
        (Decimal("-0.000"), 0, {"type": "decimal"}),
        (Decimal("9007199254740992"), 9007199254740992, {"type": "decimal"}),
        (Decimal("9007199254740993"), "9007199254740993", {"type": "decimal"}),
        (9007199254740993, "9007199254740993", {"type": "decimal"}),
        (Decimal("1.00000000000000010"), "1.0000000000000001", {"type": "decimal"}),
        (Decimal("1E+400"), "1" + "0" * 400, {"type": "decimal"}),
        (Decimal("1E-400"), "0." + "0" * 399 + "1", {"type": "decimal"}),
        ("0.1", "0.1", {"type": "string"}),
        (None, None, {"type": "null"}),
        (True, True, {"type": "boolean"}),
        (date(2026, 9, 30), "2026-09-30", {"type": "date"}),
        (
            datetime(2026, 9, 30, 8, 15),
            "2026-09-30T08:15:00",
            {"type": "timestamp", "timezone": "naive"},
        ),
        (
            datetime(2026, 9, 30, 8, 15, tzinfo=timezone(timedelta(hours=-4))),
            "2026-09-30T12:15:00+00:00",
            {"type": "timestamp", "timezone": "aware"},
        ),
        (time(8, 15, 0, 123), "08:15:00.000123", {"type": "time", "timezone": "naive"}),
        (
            time(8, 15, tzinfo=timezone(timedelta(hours=-4))),
            "12:15:00+00:00",
            {"type": "time", "timezone": "aware"},
        ),
        (timedelta(), "P0DT0H0M0S", {"type": "interval"}),
        (timedelta(days=-1, microseconds=1), "-P0DT23H59M59.999999S", {"type": "interval"}),
        (b"\x00\xff", "AP8=", {"type": "binary", "encoding": "base64"}),
        (UUID(int=0), "00000000-0000-0000-0000-000000000000", {"type": "uuid"}),
        ({"a": [None, 1.0, "2"]}, {"a": [None, 1, "2"]}, {"type": "object"}),
        ([1, "2"], [1, "2"], {"type": "array"}),
    ],
)
def test_value_policy(value: Any, expected: Any, metadata: dict[str, str]) -> None:
    result = result_rows([{"value": value}])
    assert result["rows"] == [{"value": expected}]
    assert type(result["rows"][0]["value"]) is type(expected)
    assert result["column_types"] == {"value": metadata}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        Decimal("-Infinity"),
        object(),
        {"a": Decimal("1.0000000000000001")},
        [float("nan")],
    ],
)
def test_unsupported_values_refuse_without_exposing_rows(value: Any) -> None:
    with pytest.raises(SemanticLayerError) as exc:
        result_rows([{"value": value}])
    assert exc.value.code == "RESULT_VALUE_UNSUPPORTED"
    assert exc.value.details == {}


def test_nulls_do_not_override_types_and_input_is_unchanged() -> None:
    rows = [{"a": None}, {"a": Decimal("1.0000000000000001")}, {"a": None}]
    result = result_rows(rows)
    assert result["column_types"] == {"a": {"type": "decimal"}}
    assert isinstance(rows[1]["a"], Decimal)
    assert result_rows([]) == {"rows": [], "column_types": {}}


@pytest.mark.parametrize("other", ["0.1", True, date(2026, 9, 30)])
def test_conflicting_column_types_refuse(other: Any) -> None:
    with pytest.raises(SemanticLayerError, match="JSON result contract"):
        result_rows([{"a": Decimal("1.0000000000000001")}, {"a": other}])


def test_mixed_timestamp_awareness_refuses() -> None:
    with pytest.raises(SemanticLayerError):
        result_rows([{"a": datetime(2026, 9, 30)}, {"a": datetime(2026, 9, 30, tzinfo=UTC)}])


def test_date_bucket_and_timestamp_bucket_have_one_wire_type(runtime_factory, monkeypatch) -> None:
    runtime = runtime_factory("jaffle_shop")
    column = "temporal_role.jaffle_order_time__day"
    row = {column: date(2026, 9, 30), "orders": 1}
    monkeypatch.setattr("semantic_rails.runtime._adapter_query", lambda *args, **kwargs: [row])
    query = {**QUERY, "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"}}
    try:
        duckdb_result = runtime.query(query)
        row[column] = datetime(2026, 9, 30)
        postgres_result = runtime.query(query)
        assert json_text(duckdb_result) == json_text(postgres_result)
        assert duckdb_result["column_types"][column] == {"type": "timestamp", "timezone": "naive"}
        assert duckdb_result["rows"][0][column] == "2026-09-30T00:00:00"
    finally:
        runtime.close()


def test_driver_rows_are_byte_identical_on_http_mcp_sdk_and_cli(
    runtime_factory, monkeypatch, capsys
) -> None:
    # psycopg returns these standard Python types for numeric, timestamp,
    # timestamptz, date, time, interval and bytea; no Postgres server needed.
    postgres_row = {
        "amount": Decimal("0.10"),
        "precise": Decimal("123456789012345678.123456789"),
        "orders": 42,
        "at": datetime(2026, 9, 30, 8, 15, tzinfo=timezone(timedelta(hours=-4))),
        "local": datetime(2026, 9, 30, 8, 15),
        "day": date(2026, 9, 30),
        "clock": time(8, 15),
        "duration": timedelta(days=1, seconds=2, microseconds=3),
        "missing": None,
        "text": "0.1",
        "binary": memoryview(b"\x00\xff"),
    }
    with duckdb.connect(":memory:") as connection:
        cursor = connection.execute("""
            SELECT 0.10::DOUBLE AS amount,
                123456789012345678.123456789::DECIMAL(27,9) AS precise,
                42 AS orders, (TIMESTAMPTZ '2026-09-30 12:15:00+00' AT TIME ZONE 'UTC') AS at,
                TIMESTAMP '2026-09-30 08:15:00' AS local,
                DATE '2026-09-30' AS day, TIME '08:15:00' AS clock,
                INTERVAL '1 day 2 seconds 3 microseconds' AS duration,
                NULL AS missing, '0.1' AS text, '\\x00\\xFF'::BLOB AS binary
        """)
        duckdb_row = dict(
            zip([col[0] for col in cursor.description], cursor.fetchone(), strict=True)
        )
    # Fetch the UTC clock then attach its zone without requiring optional pytz.
    duckdb_row["at"] = duckdb_row["at"].replace(tzinfo=UTC)
    runtime = runtime_factory("jaffle_shop")
    raw_rows = [duckdb_row]
    monkeypatch.setattr(
        "semantic_rails.runtime._adapter_query",
        lambda *args, **kwargs: QueryRows(raw_rows, truncated=True),
    )
    app = SemanticLayerASGIApp()
    adapter = SemanticLayerMCPAdapter(runtime)
    outputs = []
    try:
        for row in (duckdb_row, postgres_row):
            raw_rows[:] = [row]
            sdk = runtime.query(QUERY)
            assert sdk["truncated"] is True
            http, status = SemanticHTTPService(runtime).handle("POST", "/query", {"query": QUERY})
            assert status == 200
            sent = []

            async def send(message, messages=sent):
                messages.append(message)

            asyncio.run(app._send(send, 200, http))
            http = json.loads(sent[-1]["body"])
            mcp = json.loads(json_text(adapter.call_tool("execute", {"query": QUERY})))
            _print_json(sdk)
            cli = json.loads(capsys.readouterr().out)
            for payload in (sdk, http, mcp, cli):
                outputs.append(json_text({key: payload[key] for key in ("rows", "column_types")}))
        assert len(set(outputs)) == 1
        assert json.loads(outputs[0])["rows"][0]["precise"] == "123456789012345678.123456789"
        columnar = adapter.call_tool("execute", {"query": QUERY, "row_format": "columns"})
        assert columnar["column_types"] == sdk["column_types"]
        assert columnar["rows"] == [[sdk["rows"][0][key] for key in columnar["columns"]]]
    finally:
        asyncio.run(app.aclose())
        runtime.close()


def test_segment_preview_cannot_bypass_value_guard(runtime_factory, monkeypatch) -> None:
    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr(
        "semantic_rails.runtime._adapter_query",
        lambda *args, **kwargs: [{"member_count": 1, "value": float("nan")}],
    )
    try:
        with pytest.raises(SemanticLayerError) as exc:
            runtime.segment_preview("segment.jaffle.high_value_customers")
        assert exc.value.code == "RESULT_VALUE_UNSUPPORTED"
    finally:
        runtime.close()


def test_cli_ask_preserves_type_metadata(runtime_factory, monkeypatch) -> None:
    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr("semantic_rails.cli.reports._runtime_from_ref", lambda ref: runtime)
    monkeypatch.setattr(
        "semantic_rails.runtime._adapter_query",
        lambda *args, **kwargs: [{"orders": Decimal("1.0000000000000001")}],
    )
    report = ask_report(
        PackageReference(source_path=runtime.source_path, package_id="jaffle_shop"),
        question="total orders",
        execute=True,
    )
    assert report["result"]["rows"] == [{"orders": "1.0000000000000001"}]
    assert report["result"]["column_types"] == {"orders": {"type": "decimal"}}


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (1.0, "1.0"),
        (1, True),
        ("2026-09-30T00:00:00", "2026-09-30T00:00:00+00:00"),
        ({"a": 1}, {"a": "1"}),
    ],
)
def test_conformance_preserves_type_differences(expected: Any, actual: Any) -> None:
    with pytest.raises(AssertionError, match="value mismatch"):
        assert_rows_match(
            normalize_rows([{"a": expected}]), normalize_rows([{"a": actual}]), context="typed rows"
        )


def test_conformance_keeps_number_tolerance() -> None:
    assert_rows_match([{"a": 1.0}], [{"a": 1.0000001}], context="numbers")


def test_conformance_metadata_distinguishes_decimal_strings_from_text() -> None:
    reference = result_rows([{"a": Decimal("1.0000000000000001")}])
    text = result_rows([{"a": "1.0000000000000001"}])
    assert reference["rows"] == text["rows"]
    with pytest.raises(AssertionError, match="column types differ"):
        assert_column_types_match(reference, text, context="precision")


def test_aware_timestamp_outside_utc_range_refuses() -> None:
    with pytest.raises(SemanticLayerError) as exc:
        result_rows([{"a": datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))}])
    assert exc.value.code == "RESULT_VALUE_UNSUPPORTED"


def test_json_only_adapters_use_declared_types_without_guessing_text() -> None:
    columns = [
        {"field": "n", "type": "currency"},
        {"field": "t", "type": "time", "semantic_id": "temporal_role.created"},
        {"field": "d", "type": "date"},
        {"field": "clock", "type": "time"},
        {"field": "text", "type": "string"},
    ]
    typed = [
        {
            "n": Decimal("0.1"),
            "t": datetime(2026, 9, 30, tzinfo=UTC),
            "d": date(2026, 9, 30),
            "clock": time(8, 15),
            "text": "0.1",
        }
    ]
    text = [
        {
            "n": "0.10",
            "t": "2026-09-30 00:00:00+00:00",
            "d": "2026-09-30",
            "clock": "08:15:00",
            "text": "0.1",
        }
    ]
    assert result_rows(typed, output_columns=columns) == result_rows(text, output_columns=columns)
    assert result_rows(text, output_columns=columns)["column_types"]["text"] == {"type": "string"}


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("not numeric", "number"),
        ("not a date", "date"),
        ("not a clock", "time"),
        ("not a timestamp", "timestamp"),
        ("NaN", "decimal"),
    ],
)
def test_invalid_declared_values_refuse(value: str, kind: str) -> None:
    with pytest.raises(SemanticLayerError) as exc:
        result_rows([{"a": value}], output_columns=[{"field": "a", "type": kind}])
    assert exc.value.code == "RESULT_VALUE_UNSUPPORTED"


def test_conformance_distinguishes_integer_values_without_float_rounding() -> None:
    with pytest.raises(AssertionError):
        assert_rows_match([{"a": 100000000}], [{"a": 100000001}], context="integers")


def test_json_adapter_does_not_lose_precision_before_encoding() -> None:
    rows = _extract_snowflake_json_rows(
        '[{"a":123456789012345678.123456789,"b":0.1,"text":"0.1","nested":[0.1]}]'
    )
    assert rows[0]["a"] == Decimal("123456789012345678.123456789")
    result = result_rows(rows)
    assert result["rows"] == [
        {"a": "123456789012345678.123456789", "b": 0.1, "text": "0.1", "nested": [0.1]}
    ]
    assert result["column_types"]["a"] == {"type": "decimal"}
