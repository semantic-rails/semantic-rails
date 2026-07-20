from __future__ import annotations

from pathlib import Path

import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_variant_package(package_dir: Path) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "variant_demo",
                "name": "variant_demo",
                "description": "Physical variant routing demo",
                "warehouse": "duckdb",
                "default_db": "data/example.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed_example.sql"},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                }
            },
        },
    )
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "id": "entity.demo_order",
                        "name": "demo.Order",
                        "label": "Order",
                        "key": ["order_id"],
                        "model": "orders",
                    }
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "order_fact",
                "grain": ["order_id"],
                "default_variant": "tx",
                "times": {
                    "ordered_at": {
                        "id": "temporal_role.demo_order_time",
                        "dimension_id": "dimension.demo_order_ordered_at",
                        "name": "demo.Order.ordered_at",
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    }
                },
                "dimensions": {
                    "store_id": {
                        "id": "dimension.demo_store_id",
                        "column": "store_id",
                        "kind": "categorical",
                    },
                    "customer_id": {
                        "id": "dimension.demo_customer_id",
                        "column": "customer_id",
                        "kind": "categorical",
                    },
                },
                "measures": {
                    "revenue_usd": {
                        "id": "measure.demo.revenue_usd",
                        "kind": "aggregate",
                        "expr": "order_total_cents / 100.0",
                        "time": "ordered_at",
                        "rollup": "additive",
                    },
                    "order_count": {
                        "id": "measure.demo.order_count",
                        "kind": "entity_count",
                        "entity_key": "order_id",
                        "time": "ordered_at",
                        "rollup": "additive",
                    },
                },
                "variants": {
                    "tx": {
                        "relation": "order_fact",
                        "grain": {"time": "transaction", "entities": ["order"]},
                        "covers": "inherit_all",
                    },
                    "monthly": {
                        "relation": "order_monthly",
                        "grain": {"time": "month", "entities": ["order"]},
                        "time": {"role": "ordered_at", "column": "month_start"},
                        "excludes": {"entities": ["order"], "dimensions": ["customer_id"]},
                        "columns": {
                            "store_id": "store_id",
                            "revenue_usd": "revenue_usd",
                            "order_count": "order_count",
                        },
                        "eligible_time_grains": ["month", "quarter", "year"],
                        "selection": {"priority": 50},
                        "equivalence": {"kind": "exact"},
                    },
                },
            }
        },
    )


def test_model_variants_normalize_to_explicit_aggregate_relations(tmp_path: Path):
    package_dir = tmp_path / "variant_demo"
    _write_variant_package(package_dir)

    config = load_package_config(str(package_dir))

    aggregate = next(row for row in config.aggregate_relations if row.variant_id == "monthly")
    assert aggregate.model_id == "orders"
    assert aggregate.relation == "order_monthly"
    assert aggregate.temporal_role == "temporal_role.demo_order_time"
    assert aggregate.time_column == "month_start"
    assert aggregate.grain == "month"
    assert aggregate.measure_columns == {
        "measure.demo.order_count": "order_count",
        "measure.demo.revenue_usd": "revenue_usd",
    }
    assert aggregate.dimension_columns == {"dimension.demo_store_id": "store_id"}
    assert aggregate.excluded_dimensions == ["dimension.demo_customer_id"]


def test_monthly_query_routes_to_lossless_model_variant(tmp_path: Path):
    package_dir = tmp_path / "variant_demo"
    _write_variant_package(package_dir)
    config = load_package_config(str(package_dir))

    compiled = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "aggregate",
                        "measure": "measure.demo.revenue_usd",
                        "aggregation": "sum",
                    },
                    "as": "revenue",
                }
            ],
            "group_by": ["dimension.demo_store_id"],
            "time": {
                "temporal_role": "temporal_role.demo_order_time",
                "grain": "month",
            },
        },
    )

    assert "FROM order_monthly" in compiled["sql"]
    assert "FROM order_fact" not in compiled["sql"]
    assert compiled["explain"].performance_plan["aggregate_routing"]["selected"] == [
        "aggregate_relation.orders_monthly"
    ]


def test_missing_variant_dimension_falls_back_to_raw(tmp_path: Path):
    package_dir = tmp_path / "variant_demo"
    _write_variant_package(package_dir)
    config = load_package_config(str(package_dir))

    compiled = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "aggregate",
                        "measure": "measure.demo.revenue_usd",
                        "aggregation": "sum",
                    },
                    "as": "revenue",
                }
            ],
            "group_by": ["dimension.demo_customer_id"],
            "time": {
                "temporal_role": "temporal_role.demo_order_time",
                "grain": "month",
            },
        },
    )

    assert "FROM order_fact" in compiled["sql"]
    assert "FROM order_monthly" not in compiled["sql"]
    assert compiled["explain"].performance_plan["aggregate_routing"]["selected"] == []
