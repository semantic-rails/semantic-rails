"""Exact Postgres Arrow checks beyond the normalized conformance battery.

Uses the standard SR_POSTGRES_* fixture environment.
"""

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.db_parts.adbc import AdbcAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, SeedSpec
from semantic_rails.sql_preparation import ParameterSlot, PreparedQuery, finalize_parameters
from tests.semantic_rails.test_row_filters import BY_STORE, OWN_ORDERS, A, B, _package, _q

from .targets.postgres import TARGET


@pytest.fixture
def adbc():
    if TARGET.missing_env():
        pytest.skip("requires Postgres fixture environment")
    adapter = AdbcAdapter(dict(TARGET.connection_options))
    try:
        yield adapter
    finally:
        adapter.close()


def test_postgres_exact_types(adbc):
    original_zone = adbc.query("SELECT current_setting('TimeZone') z")[0]["z"]
    row = adbc.query(
        "SELECT 123456789.4500::NUMERIC(20,4) AS n, '123.4500'::TEXT AS t, "
        "TIMESTAMPTZ '2026-09-30 12:34:56.123456+05:30' AS z, "
        "INTERVAL '1 month 2 days 3.123456 seconds' AS i, NULL::NUMERIC AS missing",
        limits={"time_zone": "Asia/Kolkata"},
    )[0]
    assert type(row["n"]) is Decimal and row["n"] == Decimal("123456789.4500")
    assert row["n"].as_tuple().exponent == -4
    assert type(row["t"]) is str and row["t"] == "123.4500"
    assert row["z"].utcoffset() == timedelta(hours=5, minutes=30)
    assert row["z"].microsecond == 123456
    assert row["z"].astimezone(UTC) == datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)
    assert (row["i"].months, row["i"].days, row["i"].nanoseconds) == (1, 2, 3123456000)
    assert row["missing"] is None
    assert adbc.query("SELECT current_setting('TimeZone') z")[0]["z"] == original_zone


def test_postgres_binds_all_slot_types_without_interpolation(adbc):
    prepared = finalize_parameters(
        PreparedQuery(
            "SELECT ?::TEXT AS tenant, ?::BIGINT AS tier, ?::BOOLEAN AS active",
            parameters=(
                ParameterSlot("tenant", "string"),
                ParameterSlot("tier", "integer"),
                ParameterSlot("active", "boolean"),
            ),
        ),
        "postgres_native",
    )
    canary = "tenant' OR true --"
    assert adbc.query_prepared(prepared, parameters=(canary, 42, False)) == [
        {"tenant": canary, "tier": 42, "active": False}
    ]
    assert canary not in prepared.sql


def test_postgres_identifier_suffix_and_escape_string_bind(adbc):
    assert adbc.query("SELECT 1 AS account$1") == [{"account$1": 1}]
    prepared = finalize_parameters(
        PreparedQuery(
            r"SELECT E'it\'s' AS label, ?::TEXT AS tenant, 'x' AS other",
            parameters=(ParameterSlot("tenant", "string"),),
        ),
        "postgres_native",
    )
    value = "tenant' OR true --"
    assert adbc.query_prepared(prepared, parameters=(value,)) == [
        {"label": "it's", "tenant": value, "other": "x"}
    ]
    assert prepared.sql == r"SELECT E'it\'s' AS label, $1::TEXT AS tenant, 'x' AS other"
    assert value not in prepared.sql


def test_postgres_row_filter_isolation(adbc, tmp_path):
    root = _package(tmp_path / "filtered", [OWN_ORDERS])
    config = load_package_config(str(root))
    package = replace(
        config.package,
        warehouse="postgres",
        default_db="",
        seed=SeedSpec(),
        connection=ConnectionSpec(kind="postgres_native", options=dict(TARGET.connection_options)),
    )
    runtime = Runtime.from_config(
        replace(config, package=package), source_path=str(root), package_id="rf"
    )
    adbc.query(
        "CREATE TEMP TABLE order_fact(order_id BIGINT, customer_id TEXT, store_id TEXT, ordered_at TIMESTAMP, amount BIGINT)"
    )
    adbc.query(
        f"INSERT INTO order_fact VALUES (1, '{A}', 's1', '2026-01-10', 10), (2, '{A}', 's2', '2026-02-10', 20), (3, '{B}', 's3', '2026-01-20', 300), (4, '{B}', 's1', '2026-02-20', 400)"
    )
    runtime.set_adapter(adbc)
    try:
        results = [runtime.query(_q(BY_STORE, customer_id=customer)) for customer in (A, B, A, B)]
        for customer, result in zip((A, B, A, B), results, strict=True):
            actual = [
                (r["dimension.rf_order_store_id"], r["revenue"], r["per_order"])
                for r in result["rows"]
            ]
            assert actual == (
                [("s1", 10, 10.0), ("s2", 20, 20.0)]
                if customer == A
                else [("s1", 400, 400.0), ("s3", 300, 300.0)]
            )
            assert A not in json.dumps(result, default=str) and B not in json.dumps(
                result, default=str
            )
            assert "$1" in result["rendered_sql"] and "?" not in result["rendered_sql"]
        assert results[0]["rendered_sql"] == results[1]["rendered_sql"]
        assert runtime.query(_q(BY_STORE, customer_id="' OR true --"))["rows"] == []
        with pytest.raises(SemanticLayerError) as caught:
            runtime.query(_q(BY_STORE))
        assert caught.value.code == "POLICY_DENIED"
    finally:
        runtime.close()


def test_postgres_statement_timeout_and_recovery(adbc):
    conn = adbc._connection()
    limit_ms = 250
    # Server deadline independently of the adapter's watchdog.
    with conn.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {limit_ms}")
        started = time.monotonic()
        with pytest.raises(Exception, match="statement timeout"):
            cursor.execute("SELECT pg_sleep(5)")
            cursor.fetchall()
        elapsed_ms = (time.monotonic() - started) * 1000
        assert elapsed_ms < limit_ms + 1000
        print(f"server_timeout_ms={elapsed_ms:.3f} limit_ms={limit_ms}")
        cursor.execute("RESET statement_timeout")
    started = time.monotonic()
    with pytest.raises(SemanticLayerError) as caught:
        adbc.query("SELECT pg_sleep(5)", limits={"statement_timeout_ms": limit_ms})
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < limit_ms + 1000
    assert caught.value.code == "QUERY_EXECUTION_ERROR"
    assert adbc._conn is None
    assert adbc.query("SELECT 1 n") == [{"n": 1}]
    print(f"adapter_timeout_ms={elapsed_ms:.3f} limit_ms={limit_ms}")


def test_postgres_cancel_and_recovery(adbc):
    conn = adbc._connection()
    limit_ms = 250
    timings = {}
    with conn.cursor() as cursor:

        def cancel():
            start = time.monotonic()
            cursor.adbc_cancel()
            timings["call_ms"] = (time.monotonic() - start) * 1000

        timer = threading.Timer(limit_ms / 1000, cancel)
        started = time.monotonic()
        timer.start()
        try:
            with pytest.raises(Exception, match="cancel"):
                cursor.execute("SELECT pg_sleep(5)")
                cursor.fetchall()
        finally:
            timer.cancel()
            timer.join()
        elapsed_ms = (time.monotonic() - started) * 1000
        assert elapsed_ms < limit_ms + 1000
        assert timings["call_ms"] < limit_ms + 1000
        cursor.execute("SELECT 1 n")
        assert cursor.fetchone() == (1,)
        print(
            f"cancel_total_ms={elapsed_ms:.3f} cancel_call_ms={timings['call_ms']:.3f} limit_ms={limit_ms}"
        )


@pytest.mark.parametrize(("cap", "truncated"), [(10, True), (100, False), (101, False)])
def test_postgres_truncation(adbc, cap, truncated):
    rows = adbc.query("SELECT generate_series(1,100) n", limits={"max_rows": cap})
    assert len(rows) == min(cap, 100)
    assert rows.truncated is truncated
    assert adbc.query("SELECT 42 n") == [{"n": 42}]


def test_postgres_restores_session_zone_and_discards_failed_queries(adbc):
    zone_sql = "SELECT current_setting('TimeZone') AS zone"
    original_zone = adbc.query(zone_sql)[0]["zone"]
    with adbc._connection().cursor() as cursor:
        cursor.execute("SET TimeZone = 'America/Los_Angeles'")
    assert adbc.query(zone_sql, limits={"time_zone": "Asia/Tokyo"}) == [{"zone": "Asia/Tokyo"}]
    assert adbc.query(zone_sql) == [{"zone": "America/Los_Angeles"}]
    with pytest.raises(SemanticLayerError):
        adbc.query("SELECT 1 / 0", limits={"time_zone": "Asia/Tokyo"})
    assert adbc._conn is None
    assert adbc.query(zone_sql) == [{"zone": original_zone}]


def test_postgres_inherited_statement_timeout_is_preserved(adbc):
    limit_ms = 300
    with adbc._connection().cursor() as cursor:
        cursor.execute("SET statement_timeout = '300ms'")
    started = time.monotonic()
    with pytest.raises(SemanticLayerError) as caught:
        adbc.query("SELECT pg_sleep(2)")
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 1500
    assert caught.value.code == "QUERY_EXECUTION_ERROR"
    assert adbc._conn is None
    assert adbc.query("SELECT 1 n") == [{"n": 1}]
    print(f"inherited_timeout_ms={elapsed_ms:.3f} limit_ms={limit_ms}")


def test_postgres_plain_json_operators_and_nested_comments(adbc):
    assert adbc.query(
        "SELECT '{\"a\":1}'::jsonb ? 'a' AS present, "
        "'{\"a\":1}'::jsonb ?| ARRAY['a','b'] AS any_present, "
        "'{\"a\":1}'::jsonb ?& ARRAY['a','b'] AS all_present"
    ) == [{"present": True, "any_present": True, "all_present": False}]
    assert adbc.query("SELECT /* outer /* inner */ ? */ 1 n") == [{"n": 1}]


def test_postgres_non_iana_session_zone_returns_aware_utc(adbc):
    with adbc._connection().cursor() as cursor:
        cursor.execute("SET TimeZone = 'GMT+5'")
    row = adbc.query("SELECT TIMESTAMPTZ '2026-09-30 12:34:56.123456+05:30' AS instant")[0]
    assert row["instant"] == datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)
    assert row["instant"].utcoffset() == timedelta(0)
    assert adbc.query("SELECT current_setting('TimeZone') zone") == [{"zone": "GMT+5"}]


def test_postgres_early_stop_restores_settings_and_reuses_connection(adbc):
    conn = adbc._connection()
    with conn.cursor() as cursor:
        cursor.execute("SET statement_timeout = '5s'")
        cursor.execute("SET TimeZone = 'America/Los_Angeles'")
    rows = adbc.query(
        "SELECT generate_series(1,500000) n",
        limits={"max_rows": 10, "statement_timeout_ms": 1000, "time_zone": "Asia/Tokyo"},
    )
    assert rows == [{"n": n} for n in range(1, 11)]
    assert rows.truncated is True
    assert adbc._connection() is conn
    assert adbc.query(
        "SELECT current_setting('statement_timeout') AS timeout, current_setting('TimeZone') AS zone"
    ) == [{"timeout": "5s", "zone": "America/Los_Angeles"}]
