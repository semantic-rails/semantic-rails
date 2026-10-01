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


@pytest.mark.parametrize(
    "names",
    [
        pytest.param(
            {
                "owner": "customer",
                "parent": "region",
                "child": "store",
                "fact": "order",
                "child_entity": "store",
                "namespace": "sales",
                "time_column": "ordered_at",
            },
            id="customer-region-store-order",
        ),
        pytest.param(
            {
                "owner": "user",
                "parent": "message",
                "child": "variation",
                "fact": "event",
                "child_entity": "message_variation",
                "namespace": "engagement",
                "time_column": "event_at",
            },
            id="user-message-variation-event",
        ),
        # Reverse the original alphabetical order: child < parent < fact < owner.
        pytest.param(
            {
                "owner": "zcustomer",
                "parent": "xregion",
                "child": "wstore",
                "fact": "yorder",
                "child_entity": "wstore",
                "namespace": "sales",
                "time_column": "yorder_at",
            },
            id="reversed-name-order",
        ),
    ],
)
def test_contextual_metric_predicate_reuses_entity_graph_for_hierarchy_reduction(
    tmp_path: Path, names: dict[str, str]
):
    owner = names["owner"]
    parent = names["parent"]
    child = names["child"]
    fact = names["fact"]
    child_entity = names["child_entity"]
    child_name = "".join(part.title() for part in child_entity.split("_"))
    namespace = names["namespace"]
    time_column = names["time_column"]
    package_dir = tmp_path / f"{parent}_scope_demo"
    _write_package_header(
        package_dir,
        f"{parent}_scope_demo",
        {
            owner: {
                "id": f"entity.demo_{owner}",
                "name": f"demo.{owner.title()}",
                "label": owner.title(),
                "key": [f"{owner}_id"],
                "model": f"{owner}s",
            },
            parent: {
                "id": f"entity.demo_{parent}",
                "name": f"demo.{parent.title()}",
                "label": parent.title(),
                "key": [f"{parent}_id"],
                "model": f"{parent}s",
            },
            child_entity: {
                "id": f"entity.demo_{child_entity}",
                "name": f"demo.{child_name}",
                "label": child_entity.replace("_", " ").capitalize(),
                "key": [f"{child}_id"],
                "model": f"{child}s",
            },
            fact: {
                "id": f"entity.demo_{fact}",
                "name": f"demo.{fact.title()}",
                "label": fact.title(),
                "key": [f"{fact}_id"],
                "model": f"{fact}s",
            },
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / f"{owner}s.yml",
        {
            "model": {
                "id": f"{owner}s",
                "entity": owner,
                "relation": f"demo_{owner}",
                "grain": [f"{owner}_id"],
                "dimensions": {
                    f"{owner}_id": {
                        "id": f"dimension.demo_{owner}_id",
                        "name": f"demo.{owner.title()}.{owner}_id",
                        "label": f"{owner.title()} id",
                        "kind": "id",
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / f"{parent}s.yml",
        {
            "model": {
                "id": f"{parent}s",
                "entity": parent,
                "relation": f"demo_{parent}",
                "grain": [f"{parent}_id"],
                "dimensions": {
                    f"{parent}_id": {
                        "id": f"dimension.demo_{parent}_id",
                        "name": f"demo.{parent.title()}.{parent}_id",
                        "label": f"{parent.title()} id",
                        "kind": "id",
                    },
                    f"{parent}_name": {
                        "id": f"dimension.demo_{parent}_name",
                        "name": f"demo.{parent.title()}.{parent}_name",
                        "label": f"{parent.title()} name",
                        "kind": "categorical",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / f"{child}s.yml",
        {
            "model": {
                "id": f"{child}s",
                "entity": child_entity,
                "relation": f"demo_{child}",
                "grain": [f"{child}_id"],
                "keys": {"primary": [f"{child}_id"], "foreign": {parent: [f"{parent}_id"]}},
                "joins": {parent: {"id": f"relationship.demo_{child}_{parent}", "to": parent}},
                "dimensions": {
                    f"{child}_id": {
                        "id": f"dimension.demo_{child}_id",
                        "name": f"demo.{child_name}.{child}_id",
                        "label": f"{child.title()} id",
                        "kind": "id",
                    },
                    f"{parent}_id": {
                        "id": f"dimension.demo_{child}_{parent}_id",
                        "name": f"demo.{child_name}.{parent}_id",
                        "label": f"{child.title()} {parent} id",
                        "kind": "id",
                    },
                    f"{child}_name": {
                        "id": f"dimension.demo_{child}_name",
                        "name": f"demo.{child_name}.{child}_name",
                        "label": f"{child.title()} name",
                        "kind": "categorical",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / f"{fact}s.yml",
        {
            "model": {
                "id": f"{fact}s",
                "entity": fact,
                "relation": f"demo_{fact}",
                "grain": [f"{fact}_id"],
                "keys": {
                    "primary": [f"{fact}_id"],
                    "foreign": {owner: [f"{owner}_id"], child_entity: [f"{child}_id"]},
                },
                "joins": {
                    owner: {"id": f"relationship.demo_{fact}_{owner}", "to": owner},
                    child_entity: {
                        "id": f"relationship.demo_{fact}_{child}",
                        "to": child_entity,
                    },
                },
                "dimensions": {
                    f"{fact}_id": {
                        "id": f"dimension.demo_{fact}_id",
                        "name": f"demo.{fact.title()}.{fact}_id",
                        "label": f"{fact.title()} id",
                        "kind": "id",
                    },
                    f"{owner}_id": {
                        "id": f"dimension.demo_{fact}_{owner}_id",
                        "name": f"demo.{fact.title()}.{owner}_id",
                        "label": f"{owner.title()} id",
                        "kind": "id",
                    },
                    f"{child}_id": {
                        "id": f"dimension.demo_{fact}_{child}_id",
                        "name": f"demo.{fact.title()}.{child}_id",
                        "label": f"{child.title()} id",
                        "kind": "id",
                    },
                },
                "times": {
                    time_column: {
                        "id": f"temporal_role.demo_{fact}_time",
                        "name": f"demo.{fact.title()}.{time_column}",
                        "label": f"{fact.title()} time",
                        "column": time_column,
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": {
                    f"{fact}_count": {
                        "id": f"measure.demo.{fact}_count",
                        "name": f"{namespace}.{fact}s",
                        "label": f"{fact.title()}s",
                        "kind": "entity_count",
                        "time": time_column,
                        "publish": {"id": f"metric.{namespace}.{fact}s"},
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                f"{namespace}.{fact}s_from_{owner}s_with_2plus_{fact}s_in_period": {
                    "id": f"metric.{namespace}.{fact}s_from_{owner}s_with_2plus_{fact}s_in_period",
                    "name": f"{namespace}.{fact}s_from_{owner}s_with_2plus_{fact}s_in_period",
                    "label": f"{fact.title()}s from {owner}s with 2+ {fact}s in period",
                    "kind": "aggregate",
                    "temporal_role": f"temporal_role.demo_{fact}_time",
                    "expression": {
                        "kind": "aggregate",
                        "measure": f"measure.demo.{fact}_count",
                        "aggregation": "count_distinct",
                        "filter": {
                            "all": [
                                {
                                    "expression": {
                                        "kind": "metric_predicate",
                                        "entity": f"entity.demo_{owner}",
                                        "scope_mode": "contextual",
                                        "input": {"metric": f"metric.{namespace}.{fact}s"},
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
                        "metric": f"metric.{namespace}.{fact}s_from_{owner}s_with_2plus_{fact}s_in_period"
                    },
                    "as": f"{fact}s",
                }
            ],
            "group_by": [f"dimension.demo_{child}_name", f"dimension.demo_{parent}_name"],
            "time": {"temporal_role": f"temporal_role.demo_{fact}_time", "grain": "month"},
        },
    )

    predicate_source_sql = compiled["sql"].split(
        f'"qualified_{owner}s_month_by_{fact}s_1" AS (', 1
    )[0]
    assert f'"dimension.demo_{owner}_id"' in predicate_source_sql
    assert f'"dimension.demo_{child}_id"' in predicate_source_sql
    assert f'"dimension.demo_{parent}_id"' not in predicate_source_sql


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

    # An order reaches a plan through the customer history row valid when it was placed, or
    # through every history row of its customer. The question means the first, so the package
    # records it.
    graph = yaml.safe_load((package_dir / "graph.yml").read_text(encoding="utf-8"))
    graph["graph"]["path_preferences"] = [
        {
            "source_entity": "order",
            "target_entity": "plan",
            "relationship_path": [
                "relationship.demo_order_customer_history",
                "relationship.demo_customer_history_plan",
            ],
        }
    ]
    _write_yaml(package_dir / "graph.yml", graph)
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
