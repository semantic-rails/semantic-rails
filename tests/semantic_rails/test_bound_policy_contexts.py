"""Bound relationship shortcuts and opaque data retain policy semantics."""

from copy import deepcopy
from dataclasses import replace

import pytest

from semantic_rails import compiler, expressions, resource_access
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

MEASURE = "measure.jaffle.revenue_usd"
METRIC = "metric.sales.aov_usd"
ENTITY = "entity.jaffle_order"
DIMENSION = "dimension.jaffle_customer_id"
RELATIONSHIP = "relationship.orders_customer"


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config("configs/semantic_rails/jaffle_shop"), semantic_policies=[])


def relationship_config(config, reverse):
    if not reverse:
        return config
    rel = next(row for row in config.relationships if row.id == RELATIONSHIP)
    reversed_rel = replace(
        rel,
        source_entity=rel.target_entity,
        target_entity=rel.source_entity,
        source_column=rel.target_column,
        target_column=rel.source_column,
        source_columns=rel.target_columns,
        target_columns=rel.source_columns,
        cardinality="one_to_many",
        rollup_safe_aggregations=rel.rollup_safe_aggregations_reverse,
        rollup_safe_aggregations_reverse=rel.rollup_safe_aggregations,
    )
    return replace(
        config,
        relationships=[reversed_rel if row.id == rel.id else row for row in config.relationships],
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
@pytest.mark.parametrize("action", ["deny", "redact"])
def test_bound_relationship_shortcuts_deny_before_output(
    config, monkeypatch, reverse, restricted, scope, action
):
    config = relationship_config(config, reverse)
    context = RequestContext(
        roles=("restricted",),
        audience="restricted",
        metric_allowlist=(METRIC,) if restricted else None,
        dimension_allowlist=(DIMENSION,) if restricted else None,
    )
    query = {
        "select": [{"expression": {"metric": METRIC} if restricted else {"measure": MEASURE}}],
        "group_by": [DIMENSION],
        "policy_context": context.to_policy_context(),
    }
    # The same optimized query is valid when the selected relationship is permitted.
    assert compiler.compile_query(config, None, query)["sql"]
    if restricted:
        resource_access.ResourceAccess(config, context).enforce_query(query)

    def no_output(*args, **kwargs):
        pytest.fail("rendering or adapter access before relationship authorization")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    assert RELATIONSHIP in compiler.bind_query(config, None, query).object_ids
    policy = SemanticPolicyConfig(
        id="policy.test.shortcut",
        kind="object_access",
        object_ids=[RELATIONSHIP],
        action=action,
        **{scope: ["restricted"]},
    )
    config = replace(config, semantic_policies=[policy])
    engine = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    code = "RESOURCE_ACCESS_DENIED" if restricted else "POLICY_DENIED"
    try:
        assert engine.validate(query)["errors"][0]["code"] == code
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == code
    finally:
        engine.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_rejected_relationship_candidates_are_not_bound(config, reverse):
    config = relationship_config(config, reverse)
    selected = next(row for row in config.relationships if row.id == RELATIONSHIP)
    candidate = replace(
        selected,
        id="relationship.test.unused",
        source_column="not_a_key",
        target_column="not_a_key",
        source_columns=["not_a_key"],
        target_columns=["not_a_key"],
        path_preference=selected.path_preference + 100,
    )
    config = replace(
        config,
        relationships=[candidate, *config.relationships],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test.unused",
                kind="object_access",
                object_ids=[candidate.id],
                action="deny",
            )
        ],
    )
    query = {"select": [{"expression": {"measure": MEASURE}}], "group_by": [DIMENSION]}
    bound = compiler.bind_query(config, None, query)
    assert RELATIONSHIP in bound.object_ids
    assert candidate.id not in bound.object_ids
    engine = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["scoped_aggregate", "aggregate"])
@pytest.mark.parametrize("reference", ["metric.sales.aov_usd", "entity.jaffle_store"])
@pytest.mark.parametrize("as_list", [False, True])
def test_metric_constraint_filter_values_are_data(config, kind, reference, as_list):
    constraint = (
        "allowed_metric_filter_metrics"
        if reference.startswith("metric.")
        else "allowed_metric_filter_entities"
    )
    row = {
        "field": "dimension.jaffle_customer_type",
        "op": "in" if as_list else "=",
        "value": [reference] if as_list else reference,
    }
    expression = {"kind": kind, "measure": "measure.jaffle.order_count"}
    if kind == "scoped_aggregate":
        expression["where"] = [row]
    else:
        expression["filter"] = {"all": [row]}
    query = {
        "select": [{"expression": {"measure": MEASURE}}],
        "metric_filters": [{"expression": expression, "op": ">", "value": 0}],
    }
    assert compiler.compile_query(config, None, query)["sql"]
    assert reference not in compiler.bind_query(config, None, query).object_ids
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test.constraint",
                kind="metric_constraint",
                object_ids=[MEASURE],
                config={constraint: []},
            )
        ],
    )
    engine = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        ordinary = deepcopy(query)
        ordinary_expression = ordinary["metric_filters"][0]["expression"]
        ordinary_row = (
            ordinary_expression["where"][0]
            if kind == "scoped_aggregate"
            else ordinary_expression["filter"]["all"][0]
        )
        ordinary_row["value"] = ["new"] if as_list else "new"
        assert engine.compile(ordinary)["rendered_sql"]
        assert engine.validate(query)["ok"]
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["metrics", "entities"])
def test_metric_constraint_still_denies_real_references(config, monkeypatch, kind):
    expression = (
        {"metric": METRIC}
        if kind == "metrics"
        else {
            "kind": "metric_predicate",
            "entity": ENTITY,
            "input": {"measure": "measure.jaffle.order_count"},
            "op": ">",
            "value": 0,
        }
    )
    query = {
        "select": [{"expression": {"measure": MEASURE}}],
        "metric_filters": [
            {
                "expression": expression,
                "op": ">" if kind == "metrics" else "=",
                "value": 0 if kind == "metrics" else True,
            }
        ],
    }
    assert compiler.compile_query(config, None, query)["sql"]
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test.constraint",
                kind="metric_constraint",
                object_ids=[MEASURE],
                config={f"allowed_metric_filter_{kind}": []},
            )
        ],
    )
    engine = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def no_output(*args, **kwargs):
        pytest.fail("forbidden metric filter reached output")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["literal", "value_filter", "filter", "parameters"])
def test_structured_filter_data_is_opaque(kind):
    data = {"kind": "future_expression", "entity": ENTITY, "metric": METRIC}
    node = (
        {"kind": kind, "value": data}
        if kind != "filter"
        else {"field": DIMENSION, "op": "=", "value": data}
    )
    if kind == "parameters":
        node = {"kind": "measure", "measure": MEASURE, "parameters": data}
    assert not ({ENTITY, METRIC} & set(expressions.collect_object_references(node)))
