"""Live Snowflake ADBC row-filter isolation; run only on a credentialed runner.

Requires SR_SNOWFLAKE_CONNECTION_KIND=snowflake_adbc and skips explicitly
without SR_SNOWFLAKE_ACCOUNT/USER/PASSWORD. A configured
warehouse/driver failure is a failure, never a skip. Uses a session-local table.
"""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.db_parts.adbc import SNOWFLAKE_PROFILE, AdbcAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.result_values import result_rows
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, SeedSpec
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_row_filters import BY_STORE, OWN_ORDERS, A, B, _package, _q

from .targets.snowflake import TARGET


def setup_adbc() -> AdbcAdapter:
    """Prove the driver and connection work before testing expected query failures."""
    if os.environ.get("SR_SNOWFLAKE_CONNECTION_KIND") != "snowflake_adbc":
        pytest.skip("requires SR_SNOWFLAKE_CONNECTION_KIND=snowflake_adbc for dedicated ADBC tests")
    if TARGET.missing_env():
        pytest.skip(
            "requires SR_SNOWFLAKE_ACCOUNT, SR_SNOWFLAKE_USER and SR_SNOWFLAKE_PASSWORD on a credentialed runner"
        )
    adapter = AdbcAdapter(dict(TARGET.connection_options), profile=SNOWFLAKE_PROFILE)
    try:
        row = adapter.query(
            "SELECT TO_TIMESTAMP_NTZ('2026-01-01 12:34:56.123456')::TIMESTAMP_NTZ(6) AS instant"
        )[0]
        assert {key.lower(): value for key, value in row.items()} == {
            "instant": datetime(2026, 1, 1, 12, 34, 56, 123456)
        }
    except Exception:
        adapter.close()
        raise
    return adapter


@pytest.fixture
def adbc():
    adapter = setup_adbc()
    try:
        yield adapter
    finally:
        adapter.close()


def test_snowflake_adbc_exact_decimal_and_timestamp(adbc):
    row = adbc.query(
        "SELECT CAST(123456789012345678.123456789 AS NUMBER(38,9)) AS amount, "
        "TO_TIMESTAMP_TZ('2026-09-30 12:34:56.123456 +05:30') AS instant",
        limits={"time_zone": "Asia/Kolkata"},
    )[0]
    row = {key.lower(): value for key, value in row.items()}
    amount = Decimal("123456789012345678.123456789")
    assert type(row["amount"]) is Decimal
    assert row["amount"].as_tuple() == amount.as_tuple()
    assert row["instant"].astimezone(UTC) == datetime(2026, 9, 30, 7, 4, 56, 123456, tzinfo=UTC)
    assert row["instant"].utcoffset() == timedelta(hours=5, minutes=30)
    wire = result_rows([row], zone="Asia/Kolkata")
    assert wire["rows"] == [
        {
            "amount": str(amount),
            "instant": "2026-09-30T12:34:56.123456+05:30",
        }
    ]
    assert wire["column_types"] == {
        "amount": {"type": "decimal"},
        "instant": {"type": "timestamp", "timezone": "aware"},
    }


@pytest.mark.parametrize("year", [1600, 2500])
@pytest.mark.parametrize("kind", ["NTZ", "LTZ", "TZ"])
def test_snowflake_adbc_timestamp_overflow_refuses(adbc, year, kind):
    with pytest.raises(SemanticLayerError) as caught:
        adbc.query(
            f"SELECT TO_TIMESTAMP_{kind}('{year}-01-01 00:00:00.000000616')::TIMESTAMP_{kind}(9) AS instant"
        )
    assert caught.value.code == "QUERY_EXECUTION_ERROR"
    assert adbc._conn is None


@pytest.mark.parametrize(
    "expression",
    [
        "TO_TIME('12:34:56.123456789')::TIME(9)",
        "TO_TIMESTAMP_NTZ('2026-01-01 00:00:00.123456789')::TIMESTAMP_NTZ(9)",
        "TO_TIMESTAMP_LTZ('2026-01-01 00:00:00.123456789')::TIMESTAMP_LTZ(9)",
        "TO_TIMESTAMP_TZ('2026-01-01 00:00:00.123456789 +00:00')::TIMESTAMP_TZ(9)",
    ],
)
def test_snowflake_adbc_sub_microsecond_temporals_refuse(adbc, expression):
    with pytest.raises(SemanticLayerError) as caught:
        adbc.query(f"SELECT {expression} AS instant")
    assert caught.value.code == "RESULT_VALUE_UNSUPPORTED"
    assert adbc._conn is None


def test_snowflake_adbc_row_filter_isolation(adbc, tmp_path):
    root = _package(tmp_path / "filtered", [OWN_ORDERS])
    config = load_package_config(str(root))
    package = replace(
        config.package,
        warehouse="snowflake",
        default_db="",
        seed=SeedSpec(),
        connection=ConnectionSpec(kind="snowflake_adbc", options=adbc.options),
    )
    runtime = Runtime.from_config(
        replace(config, package=package), source_path=str(root), package_id="rf"
    )
    runtime.set_adapter(adbc)
    try:
        adbc.query(
            "CREATE TEMP TABLE order_fact(order_id BIGINT, customer_id VARCHAR, store_id VARCHAR, ordered_at TIMESTAMP_NTZ, amount NUMBER(20,4))"
        )
        adbc.query(
            f"INSERT INTO order_fact VALUES (1, '{A}', 's1', '2026-01-10', 10), (2, '{A}', 's2', '2026-02-10', 20), (3, '{B}', 's3', '2026-01-20', 300), (4, '{B}', 's1', '2026-02-20', 400)"
        )
        results = [runtime.query(_q(BY_STORE, customer_id=tenant)) for tenant in (A, B, A, B)]
        for tenant, result in zip((A, B, A, B), results, strict=True):
            # Snowflake folds unquoted aliases to upper case, as in conformance.
            rows = [
                {key.lower(): value for key, value in row.items()} for row in typed_rows(result)
            ]
            types = {key.lower(): value for key, value in result["column_types"].items()}
            assert types["revenue"] == {"type": "decimal"}
            actual = [
                (r["dimension.rf_order_store_id"], r["revenue"], r["per_order"]) for r in rows
            ]
            assert actual == (
                [("s1", 10, 10.0), ("s2", 20, 20.0)]
                if tenant == A
                else [("s1", 400, 400.0), ("s3", 300, 300.0)]
            )
            wire = json.dumps(result, default=str)
            assert A not in wire and B not in wire
            assert "?" in result["rendered_sql"]
        assert len({result["rendered_sql"] for result in results}) == 1
        assert runtime.query(_q(BY_STORE, customer_id="' OR true --"))["rows"] == []
        with pytest.raises(SemanticLayerError) as caught:
            runtime.query(_q(BY_STORE))
        assert caught.value.code == "POLICY_DENIED"
    finally:
        runtime.close()
