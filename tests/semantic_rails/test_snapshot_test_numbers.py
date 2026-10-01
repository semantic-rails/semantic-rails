"""``query_matches_snapshot`` package tests compare numbers by value."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.package_tools import _normalize_rows, _run_test, run_package_tests_report
from semantic_rails.result_values import result_rows
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.dbt_warehouse import write_orders_package

# DuckDB returns SUM over these DECIMAL(4,2) literals as Python Decimals.
SEED_SQL = """
CREATE TABLE fct_orders AS SELECT * FROM (VALUES
  (1, TIMESTAMP '2024-01-01 00:00:00', 'placed', 19.99),
  (2, TIMESTAMP '2024-01-02 00:00:00', 'shipped', 0.10)
) AS t(order_id, ordered_at, status, order_total);
"""


def test_decimal_results_match_the_numbers_written_in_yaml(tmp_path: Path) -> None:
    package = write_orders_package(tmp_path, schema="", with_customers=False)
    (package / "data" / "seed.sql").write_text(SEED_SQL, encoding="utf-8")
    snapshot = {
        "kind": "query_matches_snapshot",
        "query": {
            "version": 1,
            "select": [{"expression": {"measure": "measure.shop.order_total"}, "as": "revenue"}],
            "group_by": ["dimension.shop_order_status"],
            "limit": 5,
        },
        "expected_rows": [
            {"dimension.shop_order_status": "placed", "revenue": 19.99},
            {"dimension.shop_order_status": "shipped", "revenue": 0.1},
        ],
    }
    (package / "tests").mkdir()
    (package / "tests" / "core.yml").write_text(
        yaml.safe_dump({"tests": {"revenue_by_status": snapshot}}), encoding="utf-8"
    )

    report = run_package_tests_report(resolve_package_reference(path=str(package)))

    assert report["summary"] == {"tests_total": 1, "passed": 1, "failed": 0}, report


def test_numbers_compare_by_value_to_the_last_digit() -> None:
    assert _normalize_rows([{"x": Decimal("0.10")}]) == _normalize_rows([{"x": 0.1}])
    assert _normalize_rows([{"x": Decimal("262.00")}]) == _normalize_rows([{"x": 262}])
    assert _normalize_rows([{"x": Decimal("-0.10")}]) == _normalize_rows([{"x": -0.1}])
    assert _normalize_rows([{"x": Decimal("-262.00")}]) == _normalize_rows([{"x": -262}])
    # Rows sort by their JSON text, which put Decimal 3 before 30 but int 30 before 3.
    decimals = [{"x": Decimal("3")}, {"x": Decimal("30")}]
    assert _normalize_rows(decimals) == _normalize_rows([{"x": 30}, {"x": 3}])
    assert _normalize_rows([{"x": 1.5}]) != _normalize_rows([{"x": 2.5}])
    assert _normalize_rows([{"x": Decimal("12345678.123456789012")}]) != _normalize_rows(
        [{"x": Decimal("12345678.123456789013")}]
    )
    # 38 digits, a DECIMAL's maximum, past the default context's 28.
    wide = Decimal("12345678901234567890.123456789012345678")
    assert _normalize_rows([{"x": wide}])[0]["x"] == wide
    assert _normalize_rows([{"x": wide}]) != _normalize_rows([{"x": wide + Decimal("1E-18")}])
    assert _normalize_rows([{"flag": True}])[0]["flag"] is True  # bools stay bools


@pytest.mark.parametrize("kind", ["date", "month", "integer"])
def test_snapshots_encode_dates_buckets_and_large_integers_like_results(
    tmp_path: Path, kind: str
) -> None:
    package = write_orders_package(tmp_path, schema="", with_customers=False)
    (package / "data" / "seed.sql").write_text(
        SEED_SQL
        + "\nALTER TABLE fct_orders ADD COLUMN order_date DATE;\nUPDATE fct_orders SET order_date = CAST(ordered_at AS DATE);\nALTER TABLE fct_orders ADD COLUMN large_id BIGINT DEFAULT 9007199254740993;\n"
    )
    model_path = package / "models" / "orders.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["times"]["order_date"] = {
        "kind": "date",
        "column": "order_date",
        "class": "event_time",
    }
    model["model"]["dimensions"]["large_id"] = {"kind": "integer", "column": "large_id"}
    model_path.write_text(yaml.safe_dump(model))
    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.shop.order_count"}, "as": "orders"}],
    }
    if kind == "month":
        query["time"] = {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "month"}
        expected = [{"temporal_role.shop_order_ordered_at__month": date(2024, 1, 1), "orders": 2}]
    elif kind == "date":
        query["group_by"] = ["dimension.shop_order_order_date"]
        expected = safe_load(
            "- {dimension.shop_order_order_date: 2024-01-01, orders: 1}\n- {dimension.shop_order_order_date: 2024-01-02, orders: 1}"
        )
        assert isinstance(expected[0]["dimension.shop_order_order_date"], date)
    else:
        query["group_by"] = ["dimension.shop_order_large_id"]
        expected = [{"dimension.shop_order_large_id": 9007199254740993, "orders": 2}]
    (package / "tests").mkdir()
    (package / "tests" / "core.yml").write_text(
        yaml.safe_dump(
            {
                "tests": {
                    "typed_values": {
                        "kind": "query_matches_snapshot",
                        "query": query,
                        "expected_rows": expected,
                    }
                }
            }
        )
    )
    report = run_package_tests_report(resolve_package_reference(path=str(package)))
    assert report["summary"] == {"tests_total": 1, "passed": 1, "failed": 0}, report


def test_snapshot_does_not_coerce_numeric_looking_text(runtime_factory, monkeypatch) -> None:
    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr(
        "semantic_rails.runtime._adapter_query",
        lambda *args, **kwargs: [{"dimension.jaffle_store_name": "007", "orders": 1}],
    )
    spec = {
        "kind": "query_matches_snapshot",
        "query": {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "group_by": ["dimension.jaffle_store_name"],
        },
        "expected_rows": [{"dimension.jaffle_store_name": 7, "orders": 1}],
    }
    try:
        assert _run_test(runtime, "text", spec)["ok"] is False
    finally:
        runtime.close()


@pytest.mark.parametrize("expected", [0.1, "0.1", 1, 1.0])
def test_metric_equivalence_uses_each_response_column_types(
    runtime_factory, monkeypatch, expected
) -> None:
    runtime = runtime_factory("jaffle_shop")
    decimal = Decimal("0.1") if expected in (0.1, "0.1") else Decimal("1")
    responses = iter([result_rows([{"v": decimal}]), result_rows([{"v": expected}])])
    monkeypatch.setattr(runtime, "query", lambda query: next(responses))
    try:
        report = _run_test(runtime, "equivalent", {"kind": "metric_equals_query"})
        assert report["ok"] is (not isinstance(expected, str))
        if isinstance(expected, str):
            assert report["error"]["code"] == "METRIC_QUERY_MISMATCH"
    finally:
        runtime.close()
