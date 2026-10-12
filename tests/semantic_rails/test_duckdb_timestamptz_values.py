"""Raw zone-aware values work with the base DuckDB installation."""

from pathlib import Path

import yaml

from semantic_rails.architect_introspection import open_duckdb, profile_columns, suggest_model
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import ORDER_COUNT_QUERY, write_orders_package


def test_timestamptz_values_query_bucket_by_day_and_profile(tmp_path: Path) -> None:
    package = write_orders_package(tmp_path, schema="", with_customers=False)
    (package / "data/seed.sql").write_text(
        "CREATE TABLE fct_orders (order_id INTEGER PRIMARY KEY, ordered_at TIMESTAMPTZ);"
        "INSERT INTO fct_orders VALUES "
        "(1, TIMESTAMPTZ '2024-01-01 08:00:00-04:00'),"
        "(2, TIMESTAMPTZ '2024-01-01 12:00:00+00:00'),"
        "(3, TIMESTAMPTZ '2024-01-02 12:00:00+00:00');",
        encoding="utf-8",
    )
    model_path = package / "models/orders.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["dimensions"] = {"ordered_at": {"kind": "categorical", "column": "ordered_at"}}
    model_path.write_text(yaml.safe_dump(model), encoding="utf-8")
    runtime = Runtime.from_path(str(package))
    field = "dimension.shop_order_ordered_at"
    try:
        # A raw value keeps its zone.
        result = runtime.query({**ORDER_COUNT_QUERY, "group_by": [field]})
        assert sorted(result["rows"], key=lambda row: row[field]) == [
            {field: "2024-01-01T12:00:00+00:00", "orders": 2},
            {field: "2024-01-02T12:00:00+00:00", "orders": 1},
        ]
        assert result["column_types"][field] == {"type": "timestamp", "timezone": "aware"}
        # The time axis has a grain; a day's period is its ISO date.
        time = {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "day"}
        result = runtime.query({**ORDER_COUNT_QUERY, "time": time})
        assert sorted(result["rows"], key=lambda row: row["period"]) == [
            {"period": "2024-01-01", "period_partial": False, "orders": 2},
            {"period": "2024-01-02", "period_partial": False, "orders": 1},
        ]
        assert result["column_types"]["period"] == {"type": "date"}
    finally:
        runtime.close()
    with open_duckdb(package / "data/warehouse.duckdb") as warehouse:
        warehouse.connection.execute("SET TimeZone = 'UTC'")
        profile = profile_columns(warehouse, "fct_orders", ["ordered_at"])
        suggestion = suggest_model(warehouse, "fct_orders")
    assert profile["row_count"] == 3
    column = profile["columns"][0]
    assert column["distinct_count"] == 2
    assert column["null_count"] == 0
    assert column["min"] == "2024-01-01 12:00:00+00:00"
    assert column["max"] == "2024-01-02 12:00:00+00:00"
    assert column["samples"] == [column["min"], column["max"]]
    assert suggestion["upsert_model"]["times"]["ordered_at"]["kind"] == "timestamp"
