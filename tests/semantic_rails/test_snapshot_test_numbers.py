"""``query_matches_snapshot`` package tests compare numbers by value."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.package_tools import _normalize_rows, run_package_tests_report
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
