from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.registry import Registry


@pytest.mark.parametrize("form", ["inline", "recipe", "input_recipe"])
def test_distribution_input_contextual_predicate_refuses_changed_grain(form):
    config = load_package_config(
        str(Path(__file__).resolve().parents[1] / "integration/correctness/shop")
    )
    scoped_input = {
        "kind": "scoped_aggregate",
        "measure": "measure.shop.revenue",
        "predicates": [
            {
                "entity": "entity.shop_customer",
                "scope_mode": "contextual",
                "measure": "measure.shop.order_count",
                "op": ">=",
                "value": 2,
            }
        ],
    }
    expression = {
        "kind": "distribution",
        "function": "median",
        "over": {"kind": "entity_value", "entity": "entity.shop_order", "input": scoped_input},
    }
    recipe = next(r for r in config.metric_recipes if r.id == "metric.shop.order_revenue_median")
    if form == "input_recipe":
        input_recipe = replace(
            recipe,
            id="metric.shop.qualifying_revenue",
            expression=parse_semantic_expression(scoped_input, context="query"),
        )
        config = replace(config, metric_recipes=[*config.metric_recipes, input_recipe])
        expression["over"]["input"] = {"metric": input_recipe.id}
    if form == "recipe":
        config = replace(
            config,
            metric_recipes=[
                replace(r, expression=parse_semantic_expression(expression, context="query"))
                if r.id == recipe.id
                else r
                for r in config.metric_recipes
            ],
        )
        expression = {"metric": recipe.id}
    query = {
        "select": [{"expression": expression, "as": "median"}],
        "group_by": ["dimension.shop_order_store_id"],
    }
    # The predicate alone qualifies customer 101 within store a. Adding order_id
    # to that context would make COUNT(DISTINCT order_id) >= 2 impossible.
    with pytest.raises(SemanticLayerError, match="entity_only") as exc:
        compile_query(config, Registry(config), query)
    assert exc.value.code == "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE"


def test_distribution_entity_only_input_predicate_keeps_its_own_clock():
    config = load_package_config(
        str(Path(__file__).resolve().parents[1] / "integration/correctness/shop")
    )
    query = {
        "select": [
            {
                "as": "median",
                "expression": {
                    "kind": "distribution",
                    "function": "median",
                    "over": {
                        "kind": "entity_value",
                        "entity": "entity.shop_order",
                        "input": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.shop.revenue",
                            "predicates": [
                                {
                                    "entity": "entity.shop_customer",
                                    "scope_mode": "entity_only",
                                    "measure": "measure.shop.signup_count",
                                    "op": ">=",
                                    "value": 1,
                                }
                            ],
                        },
                    },
                },
            }
        ],
        "group_by": ["dimension.shop_order_store_id"],
        "time": {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "month"},
    }
    # A lifetime signup predicate remains valid in an order-clock distribution.
    assert compile_query(config, Registry(config), query)["sql"]


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_package_header(
    package_dir: Path, package_id: str, graph_entities: dict, *, graph_relationships: dict
) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "schema_strict": True,
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
    _write_yaml(
        package_dir / "graph.yml",
        {"graph": {"entities": graph_entities, "relationships": graph_relationships}},
    )


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
                "as": f"entity.demo_{owner}",
                "name": f"demo.{owner.title()}",
                "label": owner.title(),
                "key": [f"{owner}_id"],
                "model": f"{owner}s",
            },
            parent: {
                "as": f"entity.demo_{parent}",
                "name": f"demo.{parent.title()}",
                "label": parent.title(),
                "key": [f"{parent}_id"],
                "model": f"{parent}s",
            },
            child_entity: {
                "as": f"entity.demo_{child_entity}",
                "name": f"demo.{child_name}",
                "label": child_entity.replace("_", " ").capitalize(),
                "key": [f"{child}_id"],
                "model": f"{child}s",
            },
            fact: {
                "as": f"entity.demo_{fact}",
                "name": f"demo.{fact.title()}",
                "label": fact.title(),
                "key": [f"{fact}_id"],
                "model": f"{fact}s",
            },
        },
        graph_relationships={
            f"relationship.demo_{child}_{parent}": {
                "id": f"relationship.demo_{child}_{parent}",
                "entities": [child_entity, parent],
                "cardinality": "many_to_one",
            },
            f"relationship.demo_{fact}_{owner}": {
                "id": f"relationship.demo_{fact}_{owner}",
                "entities": [fact, owner],
                "cardinality": "many_to_one",
            },
            f"relationship.demo_{fact}_{child}": {
                "id": f"relationship.demo_{fact}_{child}",
                "entities": [fact, child_entity],
                "cardinality": "many_to_one",
            },
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / f"{owner}s.yml",
        {
            "model": {
                "id": f"{owner}s",
                "entities": {owner: {}},
                "relation": f"demo_{owner}",
                "dimensions": {
                    f"{owner}_id": {
                        "as": f"dimension.demo_{owner}_id",
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
                "entities": {parent: {}},
                "relation": f"demo_{parent}",
                "dimensions": {
                    f"{parent}_id": {
                        "as": f"dimension.demo_{parent}_id",
                        "name": f"demo.{parent.title()}.{parent}_id",
                        "label": f"{parent.title()} id",
                        "kind": "id",
                    },
                    f"{parent}_name": {
                        "as": f"dimension.demo_{parent}_name",
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
                "entities": {child_entity: {}, parent: {"expr": [f"{parent}_id"]}},
                "relation": f"demo_{child}",
                "dimensions": {
                    f"{child}_id": {
                        "as": f"dimension.demo_{child}_id",
                        "name": f"demo.{child_name}.{child}_id",
                        "label": f"{child.title()} id",
                        "kind": "id",
                    },
                    f"{parent}_id": {
                        "as": f"dimension.demo_{child}_{parent}_id",
                        "name": f"demo.{child_name}.{parent}_id",
                        "label": f"{child.title()} {parent} id",
                        "kind": "id",
                    },
                    f"{child}_name": {
                        "as": f"dimension.demo_{child}_name",
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
                "entities": {
                    fact: {},
                    owner: {"expr": [f"{owner}_id"]},
                    child_entity: {"expr": [f"{child}_id"]},
                },
                "relation": f"demo_{fact}",
                "dimensions": {
                    f"{fact}_id": {
                        "as": f"dimension.demo_{fact}_id",
                        "name": f"demo.{fact.title()}.{fact}_id",
                        "label": f"{fact.title()} id",
                        "kind": "id",
                    },
                    f"{owner}_id": {
                        "as": f"dimension.demo_{fact}_{owner}_id",
                        "name": f"demo.{fact.title()}.{owner}_id",
                        "label": f"{owner.title()} id",
                        "kind": "id",
                    },
                    f"{child}_id": {
                        "as": f"dimension.demo_{fact}_{child}_id",
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
                        "as": f"measure.demo.{fact}_count",
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
                f"{namespace}.{fact}s": {
                    "value_type": "number",
                    "id": f"metric.{namespace}.{fact}s",
                    "kind": "aggregate",
                    "measure": f"measure.demo.{fact}_count",
                },
                f"{namespace}.{fact}s_from_{owner}s_with_2plus_{fact}s_in_period": {
                    "value_type": "number",
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
                },
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
                "as": "entity.demo_customer",
                "name": "demo.Customer",
                "label": "Customer",
                "key": ["customer_id"],
                "model": "customers",
            },
            "plan": {
                "as": "entity.demo_plan",
                "name": "demo.Plan",
                "label": "Plan",
                "key": ["plan_id"],
                "model": "plans",
            },
            "customer_history": {
                "as": "entity.demo_customer_history",
                "name": "demo.CustomerHistory",
                "label": "Customer history",
                "key": ["customer_id", "valid_from"],
                "model": "customer_history",
            },
            "order": {
                "as": "entity.demo_order",
                "name": "demo.Order",
                "label": "Order",
                "key": ["order_id"],
                "model": "orders",
            },
        },
        graph_relationships={
            "relationship.demo_customer_history_customer": {
                "id": "relationship.demo_customer_history_customer",
                "entities": ["customer_history", "customer"],
                "cardinality": "one_to_one",
            },
            "relationship.demo_customer_history_plan": {
                "id": "relationship.demo_customer_history_plan",
                "entities": ["customer_history", "plan"],
                "cardinality": "many_to_one",
            },
            "relationship.demo_order_customer": {
                "id": "relationship.demo_order_customer",
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
            },
            "relationship.demo_order_customer_history": {
                "id": "relationship.demo_order_customer_history",
                "entities": ["order", "customer_history"],
                "cardinality": "many_to_one",
                "target": ["customer_id"],
                "temporal_validity": {
                    "valid_from": "demo_customer_history.valid_from",
                    "valid_to": "demo_customer_history.valid_to",
                },
            },
        },
    )
    _write_yaml(
        package_dir / "models" / "core" / "customers.yml",
        {
            "model": {
                "id": "customers",
                "entities": {"customer": {}},
                "relation": "demo_customer",
                "dimensions": {
                    "customer_id": {
                        "as": "dimension.demo_customer_id",
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
                "entities": {"plan": {}},
                "relation": "demo_plan",
                "dimensions": {
                    "plan_id": {
                        "as": "dimension.demo_plan_id",
                        "name": "demo.Plan.plan_id",
                        "label": "Plan id",
                        "kind": "id",
                    },
                    "plan_name": {
                        "as": "dimension.demo_plan_name",
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
                "entities": {
                    "customer_history": {},
                    "customer": {"expr": ["customer_id"]},
                    "plan": {"expr": ["plan_id"]},
                },
                "relation": "demo_customer_history",
                "dimensions": {
                    "customer_id": {
                        "as": "dimension.demo_customer_history_customer_id",
                        "name": "demo.CustomerHistory.customer_id",
                        "label": "Customer history customer id",
                        "kind": "id",
                    },
                    "plan_id": {
                        "as": "dimension.demo_customer_history_plan_id",
                        "name": "demo.CustomerHistory.plan_id",
                        "label": "Plan id",
                        "kind": "id",
                    },
                    "customer_status": {
                        "as": "dimension.demo_customer_status",
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
                "entities": {
                    "order": {},
                    "customer": {"expr": ["customer_id"]},
                    "customer_history": {"expr": ["customer_id"]},
                },
                "relation": "demo_order",
                "dimensions": {
                    "order_id": {
                        "as": "dimension.demo_order_id",
                        "name": "demo.Order.order_id",
                        "label": "Order id",
                        "kind": "id",
                    },
                    "customer_id": {
                        "as": "dimension.demo_order_customer_id",
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
                        "as": "measure.demo.order_count",
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
                "sales.orders": {
                    "value_type": "number",
                    "id": "metric.sales.orders",
                    "kind": "aggregate",
                    "measure": "measure.demo.order_count",
                },
                "sales.orders_from_customers_with_2plus_orders_in_period": {
                    "value_type": "number",
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
                },
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
    # The query's own grouping crosses the time-valid hop with no time, before the predicate.
    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["relationships"] == ["relationship.demo_order_customer_history"]
