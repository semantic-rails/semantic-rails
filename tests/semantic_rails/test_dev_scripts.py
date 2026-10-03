"""Dialect capture covers every supported warehouse."""

from scripts.dev import capture_dialect_sql
from semantic_rails.dialects import supported_warehouses


def test_dialect_capture_compiles_every_query_for_every_dialect():
    captured = capture_dialect_sql.capture()

    assert sorted(captured) == sorted(supported_warehouses())
    assert capture_dialect_sql.failures(captured) == []
    assert len({len(rows) for rows in captured.values()}) == 1
    # BigQuery legalizes result column names, so its mapping is part of the golden output.
    assert all(row["column_mapping"] for row in captured["bigquery"].values())
