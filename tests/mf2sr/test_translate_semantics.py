"""mf2sr keeps what a MetricFlow metric computes, or says why it can't.

Each test translates an inline MetricFlow project over one small DuckDB
table and queries the package, so a translation that loads but computes
something else fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from mf2sr import translate
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.runtime import Runtime

# Orders by month: Jan 2, Feb 3, Mar 3. Delivered: 2, 2, 1. First orders: 2, 2, 2.
ORDERS_SQL = """
CREATE TABLE fct_orders AS SELECT * FROM (VALUES
  (101, TIMESTAMP '2024-01-06 09:15:00', 'delivered', TRUE, 50.0),
  (102, TIMESTAMP '2024-01-18 14:02:00', 'delivered', TRUE, 20.0),
  (103, TIMESTAMP '2024-02-01 11:45:00', 'delivered', FALSE, 100.0),
  (104, TIMESTAMP '2024-02-03 16:20:00', 'delivered', TRUE, 10.0),
  (105, TIMESTAMP '2024-02-21 08:05:00', 'returned', TRUE, 40.0),
  (106, TIMESTAMP '2024-03-02 13:30:00', 'delivered', TRUE, 200.0),
  (107, TIMESTAMP '2024-03-12 19:55:00', 'shipped', TRUE, 30.0),
  (108, TIMESTAMP '2024-03-20 07:40:00', 'placed', FALSE, 5.0)
) AS t(order_id, ordered_at, status, is_first_order, amount);
"""
ORDERS = {
    "name": "orders",
    "node_relation": {"alias": "fct_orders"},
    "defaults": {"agg_time_dimension": "ordered_at"},
    "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
    "dimensions": [
        {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
        {"name": "status", "type": "categorical"},
        {"name": "is_first_order", "type": "categorical"},
    ],
    "measures": [
        {"name": "orders", "expr": "1", "agg": "sum"},
        {"name": "revenue", "expr": "amount", "agg": "sum"},
    ],
}
MONTH = "temporal_role.shop_order_ordered_at__month"
DELIVERED = "{{ Dimension('order__status') }} IN ('delivered')"
FIRST_ORDER = "{{ Dimension('order__is_first_order') }}"


def _translate(tmp_path: Path, metrics: list[dict[str, Any]]) -> Any:
    source = tmp_path / "src"
    source.mkdir()
    (source / "semantic.yml").write_text(
        yaml.safe_dump({"semantic_models": [ORDERS], "metrics": metrics}), encoding="utf-8"
    )
    report = translate(source, tmp_path / "out", package_id="shop")
    seed = report.package_dir / "data" / "seed_shop.sql"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text(ORDERS_SQL, encoding="utf-8")
    return report


def _metrics(report: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in (report.package_dir / "metrics").glob("*.yml"):
        merged.update(yaml.safe_load(path.read_text())["metrics"])
    return merged


def _by_month(report: Any, *names: str) -> dict[str, list[Any]]:
    """Each metric by month, one query per metric.

    Separate queries keep each check about one metric, whatever the engine
    does when metrics over one measure share a query.
    """
    engine = Runtime.from_path(str(report.package_dir))
    values: dict[str, list[Any]] = {}
    try:
        for name in names:
            rows = engine.query(
                {
                    "version": 1,
                    "select": [{"expression": {"metric": f"metric.shop.{name}"}, "as": name}],
                    "time": {
                        "temporal_role": "temporal_role.shop_order_ordered_at",
                        "grain": "month",
                    },
                    "order_by": [{"field": MONTH}],
                    "limit": 12,
                }
            )["rows"]
            values[name] = [row[name] for row in rows]
    finally:
        engine.close()
    return values


def _assert_valid(report: Any) -> None:
    parse, _ = parse_config_report(PackageReference(source_path=str(report.package_dir)))
    assert parse["ok"] is True, parse["errors"]


def test_manifest_filters_translate(tmp_path: Path) -> None:
    """semantic_manifest.json holds a metric's filters as where_filters."""
    filtered = {
        "name": "first_delivered",
        "type": "simple",
        "type_params": {"measure": {"name": "orders", "filter": None}},
        "filter": {
            "where_filters": [
                {"where_sql_template": DELIVERED},
                {"where_sql_template": FIRST_ORDER},
            ]
        },
    }
    manifest = tmp_path / "semantic_manifest.json"
    manifest.write_text(json.dumps({"semantic_models": [ORDERS], "metrics": [filtered]}))

    report = translate(manifest, tmp_path / "out", package_id="shop")

    clauses = _metrics(report)["first_delivered"]["expression"]["filter"]["all"]
    assert [clause["field"] for clause in clauses] == [
        "dimension.shop_order_status",
        "dimension.shop_order_is_first_order",
    ]


def test_metric_filters_filter_the_rows(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {"name": "orders", "type": "simple", "type_params": {"measure": "orders"}},
            {
                "name": "delivered_orders",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": DELIVERED,
            },
            {
                "name": "first_orders",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": FIRST_ORDER,
            },
            {
                "name": "first_delivered",
                "type": "simple",
                "type_params": {"measure": {"name": "orders", "filter": DELIVERED}},
                "filter": FIRST_ORDER,
            },
            {
                "name": "first_delivered_list",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": [DELIVERED, FIRST_ORDER],
            },
            {
                "name": "delivered_equals",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": "{{ Dimension('order__status') }} = 'delivered'",
            },
            {
                "name": "undelivered",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": "{{ Dimension('order__status') }} NOT IN ('delivered')",
            },
        ],
    )

    spec = _metrics(report)["delivered_orders"]["expression"]["filter"]
    assert spec == {
        "all": [{"field": "dimension.shop_order_status", "op": "in", "value": ["delivered"]}]
    }
    _assert_valid(report)
    values = _by_month(
        report,
        "orders",
        "delivered_orders",
        "first_orders",
        "first_delivered",
        "first_delivered_list",
        "delivered_equals",
        "undelivered",
    )
    assert values["orders"] == [2, 3, 3]
    assert values["delivered_orders"] == [2, 2, 1]
    assert values["first_orders"] == [2, 2, 2]
    assert values["first_delivered"] == [2, 1, 1]
    assert values["first_delivered_list"] == [2, 1, 1]
    assert values["delivered_equals"] == [2, 2, 1]
    assert values["undelivered"] == [1, 2]  # February and March; January had none


def test_ratio_filters_apply_to_their_side_and_the_metric_to_both(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {"name": "revenue", "type": "simple", "type_params": {"measure": "revenue"}},
            {"name": "orders", "type": "simple", "type_params": {"measure": "orders"}},
            {
                "name": "delivered_revenue_per_order",
                "type": "ratio",
                "type_params": {
                    "numerator": {"name": "revenue", "filter": DELIVERED},
                    "denominator": "orders",
                },
            },
            {
                "name": "first_order_delivered_revenue_per_order",
                "type": "ratio",
                "type_params": {
                    "numerator": {"name": "revenue", "filter": DELIVERED},
                    "denominator": "orders",
                },
                "filter": FIRST_ORDER,
            },
        ],
    )

    values = _by_month(
        report, "delivered_revenue_per_order", "first_order_delivered_revenue_per_order"
    )
    # Delivered revenue 70, 110, 200 over orders 2, 3, 3.
    assert values["delivered_revenue_per_order"] == pytest.approx([35.0, 110 / 3, 200 / 3])
    # First orders only: delivered revenue 70, 10, 200 over first orders 2, 2, 2.
    assert values["first_order_delivered_revenue_per_order"] == pytest.approx([35.0, 5.0, 100.0])


def test_a_ratio_whose_filter_cannot_be_kept_is_skipped(tmp_path: Path) -> None:
    """Without its filters this ratio would divide orders by orders."""
    report = _translate(
        tmp_path,
        [
            {
                "name": "odd_share",
                "type": "ratio",
                "type_params": {
                    "numerator": {"name": "orders", "filter": "{{ Entity('order') }} IS NOT NULL"},
                    "denominator": {"name": "orders", "filter": DELIVERED},
                },
            },
            {
                "name": "share_doubled",
                "type": "derived",
                "type_params": {"expr": "odd_share * 2", "metrics": [{"name": "odd_share"}]},
            },
        ],
    )

    assert "odd_share" not in report.metrics_emitted
    assert any(w.startswith("metric `odd_share`:") and "skipped" in w for w in report.warnings)
    # A metric built on a skipped one would fail at query time: skipped as well.
    assert "share_doubled" not in report.metrics_emitted
    assert any(
        w.startswith("metric `share_doubled`:") and "`odd_share`" in w for w in report.warnings
    )
    _assert_valid(report)


@pytest.mark.parametrize(
    ("filter_text", "reason"),
    [
        ("{{ Dimension('order__status') }} NOT BETWEEN 'a' AND 'm'", "NOT BETWEEN"),
        ("{{ Metric('orders', group_by=['order']) }} > 2", "metric predicate"),
        ("{{ Entity('order') }} IS NOT NULL", "entity"),
        ("{{ Dimension('order__nope') }} IN ('x')", "order__nope"),
        ("{{ Dimension('order__status') }} LIKE 'd%'", "could not parse"),
        (
            "{{ Dimension('order__ordered_at') }} BETWEEN '2024-01-01' AND '2024-02-01'",
            "time dimension",
        ),
    ],
)
def test_filters_the_engine_cannot_apply_are_reported(
    tmp_path: Path, filter_text: str, reason: str
) -> None:
    report = _translate(
        tmp_path,
        [
            {
                "name": "odd",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": filter_text,
            }
        ],
    )

    assert "expression" not in _metrics(report)["odd"]  # emitted unfiltered, as documented
    assert any(w.startswith("metric `odd`:") and reason in w for w in report.warnings), (
        report.warnings
    )
    _assert_valid(report)


def test_between_becomes_two_bounds(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {
                "name": "mid_status",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": "{{ Dimension('order__status') }} BETWEEN 'd' AND 'r'",
            }
        ],
    )

    field = "dimension.shop_order_status"
    assert _metrics(report)["mid_status"]["expression"]["filter"] == {
        "all": [
            {"field": field, "op": ">=", "value": "d"},
            {"field": field, "op": "<=", "value": "r"},
        ]
    }
    # delivered and placed fall between; returned and shipped sort after 'r'
    assert _by_month(report, "mid_status")["mid_status"] == [2, 2, 2]
