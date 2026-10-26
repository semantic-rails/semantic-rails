from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.sql_lowering import _query_metric_predicates
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry


@pytest.mark.parametrize(
    ("envelope", "accepted"),
    [
        ({"op": "=", "value": True}, True),
        ({"op": " = ", "value": True}, True),
        ({"value": True}, True),
        ({"op": "=", "value": False}, False),
        ({"op": "=", "value": 1}, False),
        ({"op": "!=", "value": True}, False),
        ({"op": "="}, False),
    ],
)
def test_predicate_collection_enforces_the_envelope_when_binding_is_bypassed(envelope, accepted):
    expression = {
        "kind": "metric_predicate",
        "entity": "entity.shop_customer",
        "input": {"measure": "measure.shop.order_count"},
        "op": ">",
        "value": 1,
    }
    plan = SimpleNamespace(query={"metric_filters": [{"expression": expression, **envelope}]})
    if accepted:
        assert _query_metric_predicates(plan) == [
            parse_semantic_expression(expression, context="query")
        ]
    else:
        with pytest.raises(SemanticLayerError) as exc:
            _query_metric_predicates(plan)
        assert exc.value.code == "INVALID_METRIC_FILTER"
        assert exc.value.details == {
            "op": envelope.get("op", "="),
            "value": envelope.get("value"),
        }


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
                                    "over": "all_time",
                                    "measure": "measure.shop.signup_count",
                                    "op": "gte",
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
    engine, _notes = read_call(query, config=config)
    assert compile_query(config, Registry(config), engine)["sql"]


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
                "id": package_id,
                "name": package_id,
                "description": f"{package_id} predicate scope demo",
                "default_db": ":memory:",
                "seed": {"kind": "sql_script", "source": "data/seed.sql", "post_sql": ""},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
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


def _entity(name: str, title: str, label: str, key: list[str], model: str) -> dict:
    return {
        name: {
            "as": f"entity.demo_{name}",
            "name": f"demo.{title}",
            "label": label,
            "key": key,
            "model": model,
        }
    }


def _many_to_one(relationship: str, many: str, one: str, **extra) -> dict:
    return {
        relationship: {
            "as": relationship,
            "entities": [many, one],
            "cardinality": "many_to_one",
            **extra,
        }
    }


def _dimension(model: str, key: str, as_id: str, label: str, **extra) -> dict:
    return {key: {"as": as_id, "name": f"demo.{model}.{key}", "label": label, **extra}}


def _time(model: str, column: str, as_id: str, label: str, temporal_class: str) -> dict:
    return _dimension(
        model, column, as_id, label, column=column, kind="timestamp", **{"class": temporal_class}
    )


def _write_model(
    package_dir: Path,
    folder: str,
    model_id: str,
    entities: dict,
    relation: str,
    dimensions: dict,
    **extra,
) -> None:
    _write_yaml(
        package_dir / "models" / folder / f"{model_id}s.yml",
        {
            "model": {
                "id": f"{model_id}s",
                "entities": entities,
                "relation": relation,
                "dimensions": dimensions,
                **extra,
            }
        },
    )


def _fact_count(namespace: str, fact: str, time_column: str) -> dict:
    return {
        f"{fact}_count": {
            "as": f"measure.demo.{fact}_count",
            "name": f"{namespace}.{fact}s",
            "label": f"{fact.title()}s",
            "kind": "entity_count",
            "times": [time_column],
        }
    }


def _write_predicate_metric(package_dir: Path, namespace: str, fact: str, owner: str) -> str:
    """A count of facts, and the facts of the owners with more than two in the query period."""
    measure = f"measure.demo.{fact}_count"
    key = f"{namespace}.{fact}s_from_{owner}s_with_2plus_{fact}s_in_period"
    predicate = {
        "kind": "metric_predicate",
        "entity": f"entity.demo_{owner}",
        "scope_mode": "contextual",
        "input": {"metric": f"metric.{namespace}.{fact}s"},
        "op": ">",
        "value": 2,
    }
    metrics = {
        f"{namespace}.{fact}s": {
            "value_type": "number",
            "as": f"metric.{namespace}.{fact}s",
            "kind": "aggregate",
            "measure": measure,
        },
        key: {
            "value_type": "number",
            "as": f"metric.{key}",
            "name": key,
            "label": f"{fact.title()}s from {owner}s with 2+ {fact}s in period",
            "kind": "aggregate",
            "temporal_role": f"temporal_role.demo_{fact}_time",
            "expression": {
                "kind": "aggregate",
                "measure": measure,
                "aggregation": "count_distinct",
                "filter": {"all": [{"expression": predicate}]},
            },
        },
    }
    _write_yaml(package_dir / "metrics.yml", {"metrics": metrics})
    return f"metric.{key}"


def _metric_query(metric: str, alias: str, group_by: list[str], time_role: str = "") -> dict:
    query = {
        "version": 1,
        "select": [{"expression": {"metric": metric}, "as": alias}],
        "group_by": group_by,
    }
    if time_role:
        query["time"] = {"temporal_role": time_role, "grain": "month"}
    return query


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
    owner, parent, child, fact = names["owner"], names["parent"], names["child"], names["fact"]
    child_entity = names["child_entity"]
    child_name = "".join(part.title() for part in child_entity.split("_"))
    namespace, time_column = names["namespace"], names["time_column"]
    owner_title, parent_title, fact_title = owner.title(), parent.title(), fact.title()
    package_dir = tmp_path / f"{parent}_scope_demo"
    _write_package_header(
        package_dir,
        f"{parent}_scope_demo",
        {
            **_entity(owner, owner_title, owner_title, [f"{owner}_id"], f"{owner}s"),
            **_entity(parent, parent_title, parent_title, [f"{parent}_id"], f"{parent}s"),
            **_entity(
                child_entity,
                child_name,
                child_entity.replace("_", " ").capitalize(),
                [f"{child}_id"],
                f"{child}s",
            ),
            **_entity(fact, fact_title, fact_title, [f"{fact}_id"], f"{fact}s"),
        },
        graph_relationships={
            **_many_to_one(f"relationship.demo_{child}_{parent}", child_entity, parent),
            **_many_to_one(f"relationship.demo_{fact}_{owner}", fact, owner),
            **_many_to_one(f"relationship.demo_{fact}_{child}", fact, child_entity),
        },
    )
    _write_model(
        package_dir,
        "core",
        owner,
        {owner: {}},
        f"demo_{owner}",
        _dimension(owner_title, f"{owner}_id", f"dimension.demo_{owner}_id", f"{owner_title} id"),
    )
    _write_model(
        package_dir,
        "core",
        parent,
        {parent: {}},
        f"demo_{parent}",
        {
            **_dimension(
                parent_title, f"{parent}_id", f"dimension.demo_{parent}_id", f"{parent_title} id"
            ),
            **_dimension(
                parent_title,
                f"{parent}_name",
                f"dimension.demo_{parent}_name",
                f"{parent_title} name",
                kind="categorical",
            ),
        },
    )
    _write_model(
        package_dir,
        "core",
        child,
        {child_entity: {}, parent: {"expr": [f"{parent}_id"]}},
        f"demo_{child}",
        {
            **_dimension(
                child_name, f"{child}_id", f"dimension.demo_{child}_id", f"{child.title()} id"
            ),
            **_dimension(
                child_name,
                f"{parent}_id",
                f"dimension.demo_{child}_{parent}_id",
                f"{child.title()} {parent} id",
            ),
            **_dimension(
                child_name,
                f"{child}_name",
                f"dimension.demo_{child}_name",
                f"{child.title()} name",
                kind="categorical",
            ),
        },
    )
    _write_model(
        package_dir,
        "core",
        fact,
        {fact: {}, owner: {"expr": [f"{owner}_id"]}, child_entity: {"expr": [f"{child}_id"]}},
        f"demo_{fact}",
        {
            **_dimension(fact_title, f"{fact}_id", f"dimension.demo_{fact}_id", f"{fact_title} id"),
            **_dimension(
                fact_title, f"{owner}_id", f"dimension.demo_{fact}_{owner}_id", f"{owner_title} id"
            ),
            **_dimension(
                fact_title,
                f"{child}_id",
                f"dimension.demo_{fact}_{child}_id",
                f"{child.title()} id",
            ),
        },
        times=_time(
            fact_title,
            time_column,
            f"temporal_role.demo_{fact}_time",
            f"{fact_title} time",
            "event_time",
        ),
        measures=_fact_count(namespace, fact, time_column),
    )
    metric = _write_predicate_metric(package_dir, namespace, fact, owner)

    config, registry = _load_config(package_dir)
    group_by = [f"dimension.demo_{child}_name", f"dimension.demo_{parent}_name"]
    query = _metric_query(metric, f"{fact}s", group_by, f"temporal_role.demo_{fact}_time")
    compiled = compile_query(config, registry, query)

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
            **_entity("customer", "Customer", "Customer", ["customer_id"], "customers"),
            **_entity("plan", "Plan", "Plan", ["plan_id"], "plans"),
            **_entity(
                "customer_history",
                "CustomerHistory",
                "Customer history",
                ["customer_id", "valid_from"],
                "customer_history",
            ),
            **_entity("order", "Order", "Order", ["order_id"], "orders"),
        },
        graph_relationships={
            **_many_to_one(
                "relationship.demo_customer_history_customer", "customer_history", "customer"
            ),
            **_many_to_one("relationship.demo_customer_history_plan", "customer_history", "plan"),
            **_many_to_one("relationship.demo_order_customer", "order", "customer"),
            **_many_to_one(
                "relationship.demo_order_customer_history",
                "order",
                "customer_history",
                target=["customer_id"],
                temporal_validity={
                    "valid_from": "demo_customer_history.valid_from",
                    "valid_to": "demo_customer_history.valid_to",
                },
            ),
        },
    )
    _write_model(
        package_dir,
        "core",
        "customer",
        {"customer": {}},
        "demo_customer",
        _dimension("Customer", "customer_id", "dimension.demo_customer_id", "Customer id"),
    )
    _write_model(
        package_dir,
        "core",
        "plan",
        {"plan": {}},
        "demo_plan",
        {
            **_dimension("Plan", "plan_id", "dimension.demo_plan_id", "Plan id"),
            **_dimension(
                "Plan", "plan_name", "dimension.demo_plan_name", "Plan name", kind="categorical"
            ),
        },
    )
    history = "CustomerHistory"
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
                    **_dimension(
                        history,
                        "customer_id",
                        "dimension.demo_customer_history_customer_id",
                        "Customer history customer id",
                    ),
                    **_dimension(
                        history, "plan_id", "dimension.demo_customer_history_plan_id", "Plan id"
                    ),
                    **_dimension(
                        history,
                        "customer_status",
                        "dimension.demo_customer_status",
                        "Customer status",
                        kind="categorical",
                    ),
                },
                "times": {
                    **_time(
                        history,
                        "valid_from",
                        "temporal_role.demo_customer_history_valid_from",
                        "History valid from",
                        "state_time",
                    ),
                    **_time(
                        history,
                        "valid_to",
                        "temporal_role.demo_customer_history_valid_to",
                        "History valid to",
                        "state_time",
                    ),
                },
            }
        },
    )
    _write_model(
        package_dir,
        "core",
        "order",
        {
            "order": {},
            "customer": {"expr": ["customer_id"]},
            "customer_history": {"expr": ["customer_id"]},
        },
        "demo_order",
        {
            **_dimension("Order", "order_id", "dimension.demo_order_id", "Order id"),
            **_dimension("Order", "customer_id", "dimension.demo_order_customer_id", "Customer id"),
        },
        times=_time(
            "Order", "ordered_at", "temporal_role.demo_order_time", "Order time", "event_time"
        ),
        measures=_fact_count("sales", "order", "ordered_at"),
    )
    metric = _write_predicate_metric(package_dir, "sales", "order", "customer")

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
    group_by = ["dimension.demo_customer_status", "dimension.demo_plan_name"]
    compiled = compile_query(
        config, registry, _metric_query(metric, "orders", group_by, "temporal_role.demo_order_time")
    )
    predicate_source_sql = compiled["sql"].split('"qualified_customers_month_by_orders_1" AS (', 1)[
        0
    ]

    assert '"dimension.demo_customer_history_customer_id"' in predicate_source_sql
    assert '"dimension.demo_plan_id"' not in predicate_source_sql

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, registry, _metric_query(metric, "orders", group_by))
    # The query's own grouping crosses the time-valid hop with no time, before the predicate.
    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["relationships"] == ["relationship.demo_order_customer_history"]
