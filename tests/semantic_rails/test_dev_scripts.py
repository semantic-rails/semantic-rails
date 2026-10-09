"""Dialect capture covers every supported warehouse."""

import pytest

from scripts.dev import capture_dialect_sql
from semantic_rails.dialects import supported_warehouses

REFUSED = "metric.sales.rolling_7d_revenue_direct"
REFUSAL = {"error": "REWRITE_NOT_SUPPORTED: no implicit calendar"}
COMPILED = {"sql": "select 1", "column_mapping": []}


def test_dialect_capture_compiles_every_query_for_every_dialect():
    captured = capture_dialect_sql.capture()

    assert sorted(captured) == sorted(supported_warehouses())
    assert capture_dialect_sql.failures(captured) == []
    # Expected refusals are recorded as rows, so every dialect captures every query.
    assert len({len(rows) for rows in captured.values()}) == 1
    # BigQuery legalizes result column names, so its mapping is part of the golden output.
    assert all(row["column_mapping"] for row in captured["bigquery"].values())


@pytest.mark.parametrize(
    ("dialect", "metric_id", "row", "failed"),
    [
        ("clickhouse", REFUSED, REFUSAL, False),
        ("clickhouse", REFUSED, {"error": "COMPILE_ERROR: no implicit calendar"}, True),
        ("duckdb", REFUSED, REFUSAL, True),
        ("clickhouse", "metric.sales.revenue", REFUSAL, True),
        ("clickhouse", REFUSED, COMPILED, True),
    ],
    ids=["listed", "other-code", "other-dialect", "other-query", "refusal-missing"],
)
def test_dialect_capture_excuses_only_listed_refusals(dialect, metric_id, row, failed):
    captured: capture_dialect_sql.Capture = {}
    for listed_dialect, listed_id in capture_dialect_sql.EXPECTED_REFUSALS:
        captured.setdefault(listed_dialect, {})[listed_id] = dict(REFUSAL)
    captured.setdefault(dialect, {})[metric_id] = row

    assert bool(capture_dialect_sql.failures(captured)) is failed
