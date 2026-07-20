from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_package_header(package_dir: Path, package_id: str, graph_entities: dict) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": package_id,
                "name": package_id,
                "description": f"{package_id} predicate scope demo",
                "default_db": ":memory:",
                "seed": {"kind": "sql_script", "source": "data/seed.sql", "post_sql": ""},
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
    _write_yaml(package_dir / "graph.yml", {"graph": {"entities": graph_entities}})


def _load_config(package_dir: Path):
    config = load_package_config(str(package_dir))
    return config, Registry(config)


def test_contextual_metric_predicate_reuses_entity_graph_for_hierarchy_reduction(tmp_path: Path):
    package_dir = tmp_path / "region_scope_demo"
    _write_package_header(
        package_dir,
        "region_scope_demo",
        {
            "customer": {
                "id": "entity.demo_customer",
                "name": "demo.Customer",
                "label": "Customer",
                "key": ["customer_id"],
                "model": "customers",
            },
            "region": {
                "id": "entity.demo_region",
                "name": "demo.Region",
                "label": "Region",
                "key": ["region_id"],
                "model": "regions",
            },
            "store": {
                "id": "entity.demo_store",
                "name": "demo.Store",
                "label": "Store",
                "key": ["store_id"],
                "model": "stores",
            },
            "order": {
                "id": "entity.demo_order",
                "name": "demo.Order",
                "label": "Order",
                "key": ["order_id"],
                "model": "orders",
            },
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "customers.yml",
        {
            "model": {
                "id": "customers",
                "entity": "customer",
                "relation": "demo_customer",
                "grain": ["customer_id"],
                "dimensions": {
                    "customer_id": {
                        "id": "dimension.demo_customer_id",
                        "name": "demo.Customer.customer_id",
                        "label": "Customer id",
                        "kind": "id",
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "regions.yml",
        {
            "model": {
                "id": "regions",
                "entity": "region",
                "relation": "demo_region",
                "grain": ["region_id"],
                "dimensions": {
                    "region_id": {
                        "id": "dimension.demo_region_id",
                        "name": "demo.Region.region_id",
                        "label": "Region id",
                        "kind": "id",
                    },
                    "region_name": {
                        "id": "dimension.demo_region_name",
                        "name": "demo.Region.region_name",
                        "label": "Region name",
                        "kind": "categorical",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "stores.yml",
        {
            "model": {
                "id": "stores",
                "entity": "store",
                "relation": "demo_store",
                "grain": ["store_id"],
                "keys": {"primary": ["store_id"], "foreign": {"region": ["region_id"]}},
                "joins": {"region": {"id": "relationship.demo_store_region", "to": "region"}},
                "dimensions": {
                    "store_id": {
                        "id": "dimension.demo_store_id",
                        "name": "demo.Store.store_id",
                        "label": "Store id",
                        "kind": "id",
                    },
                    "region_id": {
                        "id": "dimension.demo_store_region_id",
                        "name": "demo.Store.region_id",
                        "label": "Store region id",
                        "kind": "id",
                    },
                    "store_name": {
                        "id": "dimension.demo_store_name",
                        "name": "demo.Store.store_name",
                        "label": "Store name",
                        "kind": "categorical",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "demo_order",
                "grain": ["order_id"],
                "keys": {
                    "primary": ["order_id"],
                    "foreign": {"customer": ["customer_id"], "store": ["store_id"]},
                },
                "joins": {
                    "customer": {"id": "relationship.demo_order_customer", "to": "customer"},
                    "store": {"id": "relationship.demo_order_store", "to": "store"},
                },
                "dimensions": {
                    "order_id": {
                        "id": "dimension.demo_order_id",
                        "name": "demo.Order.order_id",
                        "label": "Order id",
                        "kind": "id",
                    },
                    "customer_id": {
                        "id": "dimension.demo_order_customer_id",
                        "name": "demo.Order.customer_id",
                        "label": "Customer id",
                        "kind": "id",
                    },
                    "store_id": {
                        "id": "dimension.demo_order_store_id",
                        "name": "demo.Order.store_id",
                        "label": "Store id",
                        "kind": "id",
                    },
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
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "sales.orders_from_customers_with_2plus_orders_in_period": {
                    "id": "metric.sales.orders_from_customers_with_2plus_orders_in_period",
                    "name": "sales.orders_from_customers_with_2plus_orders_in_period",
                    "label": "Orders from customers with 2+ orders in period",
                    "kind": "aggregate",
                    "temporal_role": "temporal_role.demo_order_time",
                    "expression": {
                        "kind": "aggregate",
                        "measure": "measure.demo.order_count",
                        "aggregation": "count_distinct",
                        "filter": {
                            "all": [
                                {
                                    "expression": {
                                        "kind": "metric_predicate",
                                        "entity": "entity.demo_customer",
                                        "scope_mode": "contextual",
                                        "input": {"metric": "metric.sales.orders"},
                                        "op": ">",
                                        "value": 2,
                                    }
                                }
                            ]
                        },
                    },
                }
            }
        },
    )

    config, registry = _load_config(package_dir)
    compiled = compile_query(
        config,
        registry,
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "metric": "metric.sales.orders_from_customers_with_2plus_orders_in_period"
                    },
                    "as": "orders",
                }
            ],
            "group_by": ["dimension.demo_store_name", "dimension.demo_region_name"],
            "time": {"temporal_role": "temporal_role.demo_order_time", "grain": "month"},
        },
    )

    predicate_source_sql = compiled["sql"].split('"qualified_customers_month_by_orders_1" AS (', 1)[
        0
    ]
    assert '"dimension.demo_customer_id"' in predicate_source_sql
    assert '"dimension.demo_store_id"' in predicate_source_sql
    assert '"dimension.demo_region_id"' not in predicate_source_sql


def test_contextual_metric_predicate_reuses_entity_graph_for_variation_to_message_reduction(
    tmp_path: Path,
):
    package_dir = tmp_path / "message_scope_demo"
    _write_package_header(
        package_dir,
        "message_scope_demo",
        {
            "user": {
                "id": "entity.demo_user",
                "name": "demo.User",
                "label": "User",
                "key": ["user_id"],
                "model": "users",
            },
            "message": {
                "id": "entity.demo_message",
                "name": "demo.Message",
                "label": "Message",
                "key": ["message_id"],
                "model": "messages",
            },
            "message_variation": {
                "id": "entity.demo_message_variation",
                "name": "demo.MessageVariation",
                "label": "Message variation",
                "key": ["variation_id"],
                "model": "variations",
            },
            "event": {
                "id": "entity.demo_event",
                "name": "demo.Event",
                "label": "Event",
                "key": ["event_id"],
                "model": "events",
            },
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "users.yml",
        {
            "model": {
                "id": "users",
                "entity": "user",
                "relation": "demo_user",
                "grain": ["user_id"],
                "dimensions": {
                    "user_id": {
                        "id": "dimension.demo_user_id",
                        "name": "demo.User.user_id",
                        "label": "User id",
                        "kind": "id",
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "messages.yml",
        {
            "model": {
                "id": "messages",
                "entity": "message",
                "relation": "demo_message",
                "grain": ["message_id"],
                "dimensions": {
                    "message_id": {
                        "id": "dimension.demo_message_id",
                        "name": "demo.Message.message_id",
                        "label": "Message id",
                        "kind": "id",
                    },
                    "message_name": {
                        "id": "dimension.demo_message_name",
                        "name": "demo.Message.message_name",
                        "label": "Message name",
                        "kind": "categorical",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "variations.yml",
        {
            "model": {
                "id": "variations",
                "entity": "message_variation",
                "relation": "demo_variation",
                "grain": ["variation_id"],
                "keys": {"primary": ["variation_id"], "foreign": {"message": ["message_id"]}},
                "joins": {
                    "message": {"id": "relationship.demo_variation_message", "to": "message"}
                },
                "dimensions": {
                    "variation_id": {
                        "id": "dimension.demo_variation_id",
                        "name": "demo.MessageVariation.variation_id",
                        "label": "Variation id",
                        "kind": "id",
                    },
                    "message_id": {
                        "id": "dimension.demo_variation_message_id",
                        "name": "demo.MessageVariation.message_id",
                        "label": "Variation message id",
                        "kind": "id",
                    },
                    "variation_name": {
                        "id": "dimension.demo_variation_name",
                        "name": "demo.MessageVariation.variation_name",
                        "label": "Variation name",
                        "kind": "categorical",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "events.yml",
        {
            "model": {
                "id": "events",
                "entity": "event",
                "relation": "demo_event",
                "grain": ["event_id"],
                "keys": {
                    "primary": ["event_id"],
                    "foreign": {"user": ["user_id"], "message_variation": ["variation_id"]},
                },
                "joins": {
                    "user": {"id": "relationship.demo_event_user", "to": "user"},
                    "message_variation": {
                        "id": "relationship.demo_event_variation",
                        "to": "message_variation",
                    },
                },
                "dimensions": {
                    "event_id": {
                        "id": "dimension.demo_event_id",
                        "name": "demo.Event.event_id",
                        "label": "Event id",
                        "kind": "id",
                    },
                    "user_id": {
                        "id": "dimension.demo_event_user_id",
                        "name": "demo.Event.user_id",
                        "label": "User id",
                        "kind": "id",
                    },
                    "variation_id": {
                        "id": "dimension.demo_event_variation_id",
                        "name": "demo.Event.variation_id",
                        "label": "Variation id",
                        "kind": "id",
                    },
                },
                "times": {
                    "event_at": {
                        "id": "temporal_role.demo_event_time",
                        "name": "demo.Event.event_at",
                        "label": "Event time",
                        "column": "event_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": {
                    "event_count": {
                        "id": "measure.demo.event_count",
                        "name": "engagement.events",
                        "label": "Events",
                        "kind": "entity_count",
                        "time": "event_at",
                        "publish": {"id": "metric.engagement.events"},
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "engagement.events_from_users_with_2plus_events_in_period": {
                    "id": "metric.engagement.events_from_users_with_2plus_events_in_period",
                    "name": "engagement.events_from_users_with_2plus_events_in_period",
                    "label": "Events from users with 2+ events in period",
                    "kind": "aggregate",
                    "temporal_role": "temporal_role.demo_event_time",
                    "expression": {
                        "kind": "aggregate",
                        "measure": "measure.demo.event_count",
                        "aggregation": "count_distinct",
                        "filter": {
                            "all": [
                                {
                                    "expression": {
                                        "kind": "metric_predicate",
                                        "entity": "entity.demo_user",
                                        "scope_mode": "contextual",
                                        "input": {"metric": "metric.engagement.events"},
                                        "op": ">",
                                        "value": 2,
                                    }
                                }
                            ]
                        },
                    },
                }
            }
        },
    )

    config, registry = _load_config(package_dir)
    compiled = compile_query(
        config,
        registry,
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "metric": "metric.engagement.events_from_users_with_2plus_events_in_period"
                    },
                    "as": "events",
                }
            ],
            "group_by": ["dimension.demo_variation_name", "dimension.demo_message_name"],
            "time": {"temporal_role": "temporal_role.demo_event_time", "grain": "month"},
        },
    )

    predicate_source_sql = compiled["sql"].split('"qualified_users_month_by_events_1" AS (', 1)[0]
    assert '"dimension.demo_user_id"' in predicate_source_sql
    assert '"dimension.demo_variation_id"' in predicate_source_sql
    assert '"dimension.demo_message_id"' not in predicate_source_sql


def test_contextual_metric_predicate_requires_time_anchor_for_time_varying_context_entities(
    tmp_path: Path,
):
    package_dir = tmp_path / "history_scope_demo"
    _write_package_header(
        package_dir,
        "history_scope_demo",
        {
            "customer": {
                "id": "entity.demo_customer",
                "name": "demo.Customer",
                "label": "Customer",
                "key": ["customer_id"],
                "model": "customers",
            },
            "plan": {
                "id": "entity.demo_plan",
                "name": "demo.Plan",
                "label": "Plan",
                "key": ["plan_id"],
                "model": "plans",
            },
            "customer_history": {
                "id": "entity.demo_customer_history",
                "name": "demo.CustomerHistory",
                "label": "Customer history",
                "key": ["customer_id", "valid_from"],
                "model": "customer_history",
            },
            "order": {
                "id": "entity.demo_order",
                "name": "demo.Order",
                "label": "Order",
                "key": ["order_id"],
                "model": "orders",
            },
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "customers.yml",
        {
            "model": {
                "id": "customers",
                "entity": "customer",
                "relation": "demo_customer",
                "grain": ["customer_id"],
                "dimensions": {
                    "customer_id": {
                        "id": "dimension.demo_customer_id",
                        "name": "demo.Customer.customer_id",
                        "label": "Customer id",
                        "kind": "id",
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "plans.yml",
        {
            "model": {
                "id": "plans",
                "entity": "plan",
                "relation": "demo_plan",
                "grain": ["plan_id"],
                "dimensions": {
                    "plan_id": {
                        "id": "dimension.demo_plan_id",
                        "name": "demo.Plan.plan_id",
                        "label": "Plan id",
                        "kind": "id",
                    },
                    "plan_name": {
                        "id": "dimension.demo_plan_name",
                        "name": "demo.Plan.plan_name",
                        "label": "Plan name",
                        "kind": "categorical",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "extensions" / "customer_history.yml",
        {
            "model": {
                "id": "customer_history",
                "entity": "customer_history",
                "relation": "demo_customer_history",
                "grain": ["customer_id", "valid_from"],
                "keys": {
                    "primary": ["customer_id", "valid_from"],
                    "foreign": {"customer": ["customer_id"], "plan": ["plan_id"]},
                },
                "joins": {
                    "customer": {
                        "id": "relationship.demo_customer_history_customer",
                        "to": "customer",
                    },
                    "plan": {"id": "relationship.demo_customer_history_plan", "to": "plan"},
                },
                "dimensions": {
                    "customer_id": {
                        "id": "dimension.demo_customer_history_customer_id",
                        "name": "demo.CustomerHistory.customer_id",
                        "label": "Customer history customer id",
                        "kind": "id",
                    },
                    "plan_id": {
                        "id": "dimension.demo_customer_history_plan_id",
                        "name": "demo.CustomerHistory.plan_id",
                        "label": "Plan id",
                        "kind": "id",
                    },
                    "customer_status": {
                        "id": "dimension.demo_customer_status",
                        "name": "demo.CustomerHistory.customer_status",
                        "label": "Customer status",
                        "kind": "categorical",
                    },
                },
                "times": {
                    "valid_from": {
                        "id": "temporal_role.demo_customer_history_valid_from",
                        "name": "demo.CustomerHistory.valid_from",
                        "label": "History valid from",
                        "column": "valid_from",
                        "kind": "timestamp",
                        "class": "state_time",
                    },
                    "valid_to": {
                        "id": "temporal_role.demo_customer_history_valid_to",
                        "name": "demo.CustomerHistory.valid_to",
                        "label": "History valid to",
                        "column": "valid_to",
                        "kind": "timestamp",
                        "class": "state_time",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "demo_order",
                "grain": ["order_id"],
                "keys": {
                    "primary": ["order_id"],
                    "foreign": {"customer": ["customer_id"], "customer_history": ["customer_id"]},
                },
                "joins": {
                    "customer": {"id": "relationship.demo_order_customer", "to": "customer"},
                    "customer_history": {
                        "id": "relationship.demo_order_customer_history",
                        "to": "customer_history",
                        "target": ["customer_id"],
                        "temporal_validity": {
                            "valid_from": "demo_customer_history.valid_from",
                            "valid_to": "demo_customer_history.valid_to",
                        },
                    },
                },
                "dimensions": {
                    "order_id": {
                        "id": "dimension.demo_order_id",
                        "name": "demo.Order.order_id",
                        "label": "Order id",
                        "kind": "id",
                    },
                    "customer_id": {
                        "id": "dimension.demo_order_customer_id",
                        "name": "demo.Order.customer_id",
                        "label": "Customer id",
                        "kind": "id",
                    },
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
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "sales.orders_from_customers_with_2plus_orders_in_period": {
                    "id": "metric.sales.orders_from_customers_with_2plus_orders_in_period",
                    "name": "sales.orders_from_customers_with_2plus_orders_in_period",
                    "label": "Orders from customers with 2+ orders in period",
                    "kind": "aggregate",
                    "temporal_role": "temporal_role.demo_order_time",
                    "expression": {
                        "kind": "aggregate",
                        "measure": "measure.demo.order_count",
                        "aggregation": "count_distinct",
                        "filter": {
                            "all": [
                                {
                                    "expression": {
                                        "kind": "metric_predicate",
                                        "entity": "entity.demo_customer",
                                        "scope_mode": "contextual",
                                        "input": {"metric": "metric.sales.orders"},
                                        "op": ">",
                                        "value": 2,
                                    }
                                }
                            ]
                        },
                    },
                }
            }
        },
    )

    config, registry = _load_config(package_dir)
    compiled = compile_query(
        config,
        registry,
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "metric": "metric.sales.orders_from_customers_with_2plus_orders_in_period"
                    },
                    "as": "orders",
                }
            ],
            "group_by": ["dimension.demo_customer_status", "dimension.demo_plan_name"],
            "time": {"temporal_role": "temporal_role.demo_order_time", "grain": "month"},
        },
    )
    predicate_source_sql = compiled["sql"].split('"qualified_customers_month_by_orders_1" AS (', 1)[
        0
    ]

    assert '"dimension.demo_customer_history_customer_id"' in predicate_source_sql
    assert '"dimension.demo_plan_id"' not in predicate_source_sql

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(
            config,
            registry,
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "metric": "metric.sales.orders_from_customers_with_2plus_orders_in_period"
                        },
                        "as": "orders",
                    }
                ],
                "group_by": ["dimension.demo_customer_status", "dimension.demo_plan_name"],
            },
        )
    assert exc.value.code == "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE"
