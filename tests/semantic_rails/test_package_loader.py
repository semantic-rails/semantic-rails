from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_minimal_package(
    package_dir: Path,
    *,
    defaults_extra: dict | None = None,
    model_extra: dict | None = None,
    measure_extra: dict | None = None,
    metric_extra: dict | None = None,
) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "operational_demo",
                "name": "operational_demo",
                "description": "Operational metadata demo",
                "default_db": "data/example.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed_example.sql", "post_sql": ""},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                },
                **dict(defaults_extra or {}),
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
                "times": {
                    "ordered_at": {
                        "id": "temporal_role.demo_order_time",
                        "name": "demo.Order.ordered_at",
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": {
                    "order_count": {
                        "id": "measure.demo.order_count",
                        "name": "sales.orders",
                        "label": "Orders",
                        "kind": "entity_count",
                        "time": "ordered_at",
                        "publish": {"id": "metric.sales.orders"},
                        **dict(measure_extra or {}),
                    }
                },
                **dict(model_extra or {}),
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "sales.orders_copy": {
                    "id": "metric.sales.orders_copy",
                    "name": "sales.orders_copy",
                    "label": "Orders copy",
                    "kind": "derived",
                    "temporal_role": "temporal_role.demo_order_time",
                    "expression": {"kind": "metric", "metric": "metric.sales.orders"},
                    **dict(metric_extra or {}),
                }
            }
        },
    )


def test_loader_recurses_models_and_metrics_directories(tmp_path: Path):
    package_dir = tmp_path / "recursive_demo"

    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "recursive_demo",
                "name": "recursive_demo",
                "description": "Recursive loader demo",
                "default_db": "data/example.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed_example.sql", "post_sql": ""},
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
                    "customer": {
                        "id": "entity.demo_customer",
                        "name": "demo.Customer",
                        "label": "Customer",
                        "key": ["customer_id"],
                        "model": "customers",
                    },
                    "order": {
                        "id": "entity.demo_order",
                        "name": "demo.Order",
                        "label": "Order",
                        "key": ["order_id"],
                        "model": "orders",
                    },
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "dimensions" / "customers.yml",
        {
            "model": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer_dim",
                "grain": ["customer_id"],
                "dimensions": {
                    "country": {
                        "id": "dimension.demo_customer_country",
                        "name": "demo.Customer.country",
                        "label": "Customer country",
                        "kind": "categorical",
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "facts" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "order_fact",
                "grain": ["order_id"],
                "keys": {
                    "primary": ["order_id"],
                    "foreign": {"customer": ["customer_id"]},
                },
                "times": {
                    "ordered_at": {
                        "id": "temporal_role.demo_order_time",
                        "name": "demo.Order.ordered_at",
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": {
                    "order_count": {
                        "id": "measure.demo.order_count",
                        "name": "sales.orders",
                        "label": "Orders",
                        "kind": "entity_count",
                        "time": "ordered_at",
                        "publish": {"id": "metric.sales.orders"},
                    }
                },
                "joins": {"customer": {"to": "customer"}},
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "sales.orders_cumulative": {
                    "id": "metric.sales.orders_cumulative",
                    "name": "sales.orders_cumulative",
                    "label": "Cumulative orders",
                    "kind": "cumulative",
                    "temporal_role": "temporal_role.demo_order_time",
                    "expression": {
                        "kind": "cumulative",
                        "input": {"kind": "metric", "metric": "metric.sales.orders"},
                    },
                }
            }
        },
    )
    _write_yaml(
        package_dir / "metrics" / "sales" / "orders_copy.yml",
        {
            "metric": {
                "id": "metric.sales.orders_copy",
                "name": "sales.orders_copy",
                "label": "Orders copy",
                "kind": "derived",
                "temporal_role": "temporal_role.demo_order_time",
                "expression": {"kind": "metric", "metric": "metric.sales.orders"},
            }
        },
    )

    config = load_package_config(str(package_dir))

    assert config.version == 1
    assert any(row.id == "entity.demo_customer" for row in config.entities)
    assert any(row.id == "entity.demo_order" for row in config.entities)
    assert any(row.id == "dimension.demo_customer_country" for row in config.dimensions)
    assert any(row.id == "temporal_role.demo_order_time" for row in config.temporal_roles)
    assert any(row.id == "measure.demo.order_count" for row in config.measures)
    assert any(row.id == "metric.sales.orders" for row in config.metric_recipes)
    assert any(row.id == "metric.sales.orders_cumulative" for row in config.metric_recipes)
    assert any(row.id == "metric.sales.orders_copy" for row in config.metric_recipes)


def test_starter_normalizes_to_stable_package_config():
    config = load_package_config(
        str(REPO_ROOT / "configs" / "examples" / "semantic_rails_package_starter.yml")
    )

    assert config.version == 1
    assert config.package.package_id == "shop_starter"
    assert config.package.name == "shop_starter"

    entities = {row.id: row for row in config.entities}
    assert set(entities) == {
        "entity.shop_customer",
        "entity.shop_order",
        "entity.shop_order_item",
        "entity.shop_product",
    }
    assert entities["entity.shop_customer"].name == "shop.Customer"
    assert entities["entity.shop_order"].key == ["order_id"]

    dimensions = {row.id: row for row in config.dimensions}
    assert {
        "dimension.shop_customer_customer_type",
        "dimension.shop_customer_first_ordered_at",
        "dimension.shop_order_channel",
        "dimension.shop_order_ordered_at",
    }.issubset(dimensions)
    assert dimensions["dimension.shop_order_channel"].name == "shop.Order.channel"

    temporal_roles = {row.id: row for row in config.temporal_roles}
    assert (
        temporal_roles["temporal_role.shop_order_ordered_at"].dimension
        == "dimension.shop_order_ordered_at"
    )

    measures = {row.id: row for row in config.measures}
    assert set(measures) == {
        "measure.shop.order_count",
        "measure.shop.revenue_usd",
        "measure.shop.line_revenue_usd",
    }
    assert measures["measure.shop.revenue_usd"].default_aggregation == "sum"

    metrics = {row.id: row for row in config.metric_recipes}
    assert {
        "metric.shop.revenue_usd",
        "metric.shop.aov_usd",
        "metric.shop.revenue_per_item_usd",
    }.issubset(metrics)
    # The starter demonstrates kind: ratio with direct numerator/denominator.
    aov = metrics["metric.shop.aov_usd"]
    assert aov.kind == "ratio"


def test_jaffle_shop_package_surface_remains_stable():
    package_dir = REPO_ROOT / "configs" / "semantic_rails" / "jaffle_shop"
    raw_package = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
    config = load_package_config(str(package_dir))

    assert raw_package["schema_version"] == 1
    assert config.version == 1
    assert config.package.package_id == "jaffle_shop"
    assert config.package.default_db == "data/jaffle_shop.duckdb"
    assert {
        "entities": len(config.entities),
        "dimensions": len(config.dimensions),
        "temporal_roles": len(config.temporal_roles),
        "relationships": len(config.relationships),
        "measures": len(config.measures),
        "metric_recipes": len(config.metric_recipes),
        "segments": len(config.segments),
        "semantic_policies": len(config.semantic_policies),
        "semantic_caveats": len(config.semantic_caveats),
    } == {
        "entities": 13,
        "dimensions": 67,
        "temporal_roles": 18,
        "relationships": 16,
        "measures": 41,
        # Metric count: 24 curated metrics (was 27 before pre-launch
        # cleanup; 3 redundant semi_additive wrappers — rolling_7d_revenue,
        # revenue_mtd, prior_period_revenue — were dropped because
        # derived_metrics.yml ships direct query-time counterparts).
        # Auto-publish was already removed in the v1 migration.
        "metric_recipes": 25,
        "segments": 1,
        # plan_constraint policy was a runtime no-op and was dropped.
        # The canonical kinds (package_release, object_visibility,
        # object_access, protected_object, metric_constraint) are each
        # demonstrated in policies.yml.
        "semantic_policies": 5,
        "semantic_caveats": 1,
    }
    assert any(
        row.id == "entity.jaffle_order" and row.name == "jaffle.Order" for row in config.entities
    )
    # Measure ID and name are package-namespaced (jaffle.order_count); a
    # measure is a primitive, not a curated metric, and it does not borrow
    # the metric's `sales.*` public namespace.
    assert any(
        row.id == "measure.jaffle.order_count" and row.name == "jaffle.order_count"
        for row in config.measures
    )
    # Asserts that a curated metric in the sales namespace survives.
    assert any(row.id == "metric.sales.aov_usd" for row in config.metric_recipes)


def test_package_registry_falls_back_to_installed_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    installed_root = tmp_path / "share" / "semantic-rails"
    package_dir = installed_root / "configs" / "semantic_rails" / "jaffle_shop"
    package_dir.mkdir(parents=True)
    (package_dir / "package.yml").write_text("schema_version: 1\n", encoding="utf-8")

    monkeypatch.setattr(config_module, "repo_root", lambda: str(tmp_path / "site-packages"))
    monkeypatch.setattr(
        config_module.sysconfig, "get_path", lambda name: str(tmp_path) if name == "data" else ""
    )
    config_module.list_package_paths.cache_clear()
    try:
        assert config_module.resolve_repo_path("configs/semantic_rails") == str(
            installed_root / "configs" / "semantic_rails"
        )
        assert config_module.list_package_paths() == {"jaffle_shop": str(package_dir)}
    finally:
        config_module.list_package_paths.cache_clear()


def test_loader_accepts_snowflake_package_without_duckdb_seed(tmp_path: Path):
    package_dir = tmp_path / "snowflake_loader_demo"

    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "snowflake_loader_demo",
                "name": "snowflake_loader_demo",
                "description": "Snowflake loader demo",
                "warehouse": "snowflake",
                "connection": {"kind": "snowflake_cli", "name": "semantic_views_trial"},
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
                        "key": ["O_ORDERKEY"],
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
                "relation": "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS",
                "grain": ["O_ORDERKEY"],
                "times": {
                    "order_date": {
                        "id": "temporal_role.demo_order_date",
                        "name": "demo.Order.order_date",
                        "label": "Order date",
                        "column": "O_ORDERDATE",
                        "kind": "date",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": {
                    "order_count": {
                        "id": "measure.demo.order_count",
                        "name": "sales.orders",
                        "label": "Orders",
                        "description": "Orders",
                        "kind": "entity_count",
                        "time": "order_date",
                        "topics": ["orders"],
                        "publish": {"id": "metric.sales.orders"},
                    }
                },
            }
        },
    )
    _write_yaml(package_dir / "metrics.yml", {"metrics": {}})

    config = load_package_config(str(package_dir))

    assert config.package.warehouse == "snowflake"
    assert config.package.connection.kind == "snowflake_cli"
    assert config.package.connection.name == "semantic_views_trial"
    assert config.package.default_db == ""


def test_loader_applies_operational_metadata_contract_and_inheritance(tmp_path: Path):
    package_dir = tmp_path / "operational_demo"
    _write_minimal_package(
        package_dir,
        defaults_extra={
            "operational": {
                "measure": {
                    "fields": {
                        "owner": {"type": "string", "required": True},
                        "team": {"type": "string", "required": False},
                        "tags": {"type": "string_list", "required": False},
                    }
                },
                "metric": {
                    "fields": {
                        "owner": {"type": "string", "required": True},
                        "team": {"type": "string", "required": False},
                        "tags": {"type": "string_list", "required": False},
                        "verified": {"type": "boolean", "required": False},
                    }
                },
            }
        },
        model_extra={"operational_defaults": {"owner": "finance", "team": "growth"}},
        measure_extra={
            "operational": {"tags": ["core"]},
            "publish": {
                "id": "metric.sales.orders",
                "operational": {"verified": True},
            },
        },
        metric_extra={"operational": {"owner": "curated", "team": "strategy", "verified": False}},
    )

    config = load_package_config(str(package_dir))

    measure = next(row for row in config.measures if row.id == "measure.demo.order_count")
    auto_metric = next(row for row in config.metric_recipes if row.id == "metric.sales.orders")
    curated_metric = next(
        row for row in config.metric_recipes if row.id == "metric.sales.orders_copy"
    )

    assert measure.operational == {"owner": "finance", "team": "growth", "tags": ["core"]}
    assert auto_metric.operational == {
        "owner": "finance",
        "team": "growth",
        "tags": ["core"],
        "verified": True,
    }
    assert curated_metric.operational == {"owner": "curated", "team": "strategy", "verified": False}
    assert config.operational_contract["metric"]["fields"]["verified"]["type"] == "boolean"


def test_loader_rejects_operational_metadata_without_contract(tmp_path: Path):
    package_dir = tmp_path / "operational_demo"
    _write_minimal_package(
        package_dir,
        model_extra={"operational_defaults": {"owner": "finance"}},
    )

    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package_dir))

    assert exc.value.code == "INVALID_CONFIG"
    assert "operational_defaults" in str(exc.value)


def test_loader_rejects_missing_required_operational_fields(tmp_path: Path):
    package_dir = tmp_path / "operational_demo"
    _write_minimal_package(
        package_dir,
        defaults_extra={
            "operational": {
                "metric": {
                    "fields": {
                        "owner": {"type": "string", "required": True},
                    }
                }
            }
        },
    )

    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package_dir))

    assert exc.value.code == "INVALID_CONFIG"
    assert "missing required operational fields" in str(exc.value)


def test_loader_rejects_inherited_measure_fields_not_declared_for_metrics(tmp_path: Path):
    package_dir = tmp_path / "operational_demo"
    _write_minimal_package(
        package_dir,
        defaults_extra={
            "operational": {
                "measure": {
                    "fields": {
                        "owner": {"type": "string", "required": True},
                        "tags": {"type": "string_list", "required": False},
                    }
                },
                "metric": {
                    "fields": {
                        "owner": {"type": "string", "required": True},
                    }
                },
            }
        },
        model_extra={"operational_defaults": {"owner": "finance"}},
        measure_extra={"operational": {"tags": ["core"]}},
    )

    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package_dir))

    assert exc.value.code == "INVALID_CONFIG"
    assert "is not declared in defaults.operational.metric.fields" in str(exc.value)


def test_loader_rejects_duplicate_metric_ids(tmp_path: Path):
    package_dir = tmp_path / "duplicate_metric_demo"
    _write_minimal_package(
        package_dir,
        metric_extra={"id": "metric.sales.orders"},
    )

    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package_dir))

    assert exc.value.code == "INVALID_CONFIG"
    assert "duplicate metric id 'metric.sales.orders'" in str(exc.value)


def test_loader_rejects_duplicate_entity_ids(tmp_path: Path):
    package_dir = tmp_path / "duplicate_entity_demo"
    _write_minimal_package(package_dir)
    graph_path = package_dir / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    graph["graph"]["entities"]["invoice"] = {
        "id": "entity.demo_order",
        "name": "demo.Invoice",
        "label": "Invoice",
        "key": ["order_id"],
        "model": "orders",
    }
    _write_yaml(graph_path, graph)

    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package_dir))

    assert exc.value.code == "INVALID_CONFIG"
    assert "duplicate object id 'entity.demo_order'" in str(exc.value)


def test_loader_rejects_duplicate_object_ids_across_kinds(tmp_path: Path):
    package_dir = tmp_path / "duplicate_object_demo"
    _write_minimal_package(
        package_dir,
        model_extra={
            "dimensions": {
                "bad_dimension": {
                    "id": "entity.demo_order",
                    "name": "demo.Order.bad_dimension",
                    "label": "Bad dimension",
                    "kind": "categorical",
                }
            }
        },
    )

    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package_dir))

    assert exc.value.code == "INVALID_CONFIG"
    assert "duplicate object id 'entity.demo_order'" in str(exc.value)
