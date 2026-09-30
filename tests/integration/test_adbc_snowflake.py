"""Live Snowflake ADBC row-filter isolation; run only on a credentialed runner.

Skips explicitly without SR_SNOWFLAKE_ACCOUNT/USER/PASSWORD. A configured
warehouse/driver failure is a failure, never a skip. Uses a session-local table.
"""

import json
import os
from dataclasses import replace

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.db_parts.adbc import SNOWFLAKE_PROFILE, AdbcAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, SeedSpec
from tests.semantic_rails.test_row_filters import BY_STORE, OWN_ORDERS, A, B, _package, _q

from .targets.snowflake import TARGET


@pytest.fixture
def adbc():
    if TARGET.missing_env():
        pytest.skip(
            "requires SR_SNOWFLAKE_ACCOUNT, SR_SNOWFLAKE_USER and SR_SNOWFLAKE_PASSWORD on a credentialed runner"
        )
    options = dict(TARGET.connection_options)
    if path := os.environ.get("SR_SNOWFLAKE_ADBC_DRIVER_PATH"):
        options["driver_path"] = path
    adapter = AdbcAdapter(options, profile=SNOWFLAKE_PROFILE)
    try:
        yield adapter
    finally:
        adapter.close()


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
            rows = [{key.lower(): value for key, value in row.items()} for row in result["rows"]]
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
