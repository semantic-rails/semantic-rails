"""Bound relationship shortcuts and opaque data retain policy semantics."""

import shutil
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
    # The filter reads orders and customers; only its data value names a store.
    allowed = [] if reference.startswith("metric.") else [ENTITY, "entity.jaffle_customer"]
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
                config={constraint: allowed},
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


CUSTOMER = "entity.jaffle_customer"
STORE = "entity.jaffle_store"
CUSTOMER_COUNT = "measure.jaffle.customer_count"


def _count_where_not_zero(column):
    return {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": {"kind": "not_in", "expr": column, "values": [0]},
    }


def _orders_at_store(field):
    return {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.order_count",
        "where": [{"field": field, "op": "=", "value": "Brooklyn"}],
    }


# Each metric filter spells one entity it reads: (expression, that entity, others read).
FILTER_SPELLINGS = {
    "table_column": (
        _count_where_not_zero({"kind": "column", "table": "jaffle_order", "column": "order_id"}),
        ENTITY,
        [],
    ),
    "entity_column": (
        _count_where_not_zero({"kind": "column", "entity": ENTITY, "column": "order_id"}),
        ENTITY,
        [],
    ),
    "table_value": (
        {
            "kind": "aggregate_if",
            "aggregation": "max",
            "condition": {"kind": "literal", "value": True},
            "value": {"kind": "column", "table": "jaffle_order", "column": "order_id"},
        },
        ENTITY,
        [],
    ),
    "dimension_id": (_orders_at_store("dimension.jaffle_store_name"), STORE, [ENTITY]),
    "dimension_label": (_orders_at_store("Store name"), STORE, [ENTITY]),
    "dimension_name": (_orders_at_store("jaffle.Store.store_name"), STORE, [ENTITY]),
    "aggregate_filter": (
        {
            "kind": "aggregate",
            "measure": "measure.jaffle.order_count",
            "filter": {
                "all": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}]
            },
        },
        STORE,
        [ENTITY],
    ),
    "measure": ({"measure": "measure.jaffle.order_count"}, ENTITY, []),
    "metric": ({"metric": METRIC}, ENTITY, []),
    "predicate_input": (
        {
            "kind": "metric_predicate",
            "entity": CUSTOMER,
            "input": {"measure": "measure.jaffle.order_count"},
            "op": ">",
            "value": 1,
        },
        ENTITY,
        [],
    ),
}

# Filters that read only the governed measure's own entity.
CUSTOMER_FILTERS = {
    "table_column": _count_where_not_zero(
        {"kind": "column", "table": "jaffle_customer", "column": "customer_id"}
    ),
    "entity_column": _count_where_not_zero(
        {"kind": "column", "entity": CUSTOMER, "column": "customer_id"}
    ),
    "dimension": {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.lifetime_order_count",
        "where": [{"field": "dimension.jaffle_customer_type", "op": "=", "value": "returning"}],
    },
    "measure": {"measure": "measure.jaffle.lifetime_order_count"},
    "predicate": {
        "kind": "metric_predicate",
        "entity": CUSTOMER,
        "input": {"measure": "measure.jaffle.lifetime_spend_usd"},
        "op": ">",
        "value": 100,
    },
}


def _customer_query(expression):
    predicate = expression.get("kind") == "metric_predicate"
    return {
        "select": [{"expression": {"measure": CUSTOMER_COUNT}, "as": "customers"}],
        "group_by": ["dimension.jaffle_customer_type"],
        "metric_filters": [
            {
                "expression": deepcopy(expression),
                "op": "=" if predicate else ">",
                "value": True if predicate else 0,
            }
        ],
    }


def _constrained(config, constraint, governed=CUSTOMER_COUNT):
    """``governed=None`` makes the policy package-wide."""
    policy = SemanticPolicyConfig(
        id="policy.test.filter_entities",
        kind="metric_constraint",
        object_ids=[governed] if governed else [],
        config=constraint,
    )
    return Runtime.from_config(
        replace(config, semantic_policies=[policy]),
        source_path="configs/semantic_rails/jaffle_shop",
    )


def _assert_denied_before_output(engine, monkeypatch, query):
    def no_output(*args, **kwargs):
        pytest.fail("constrained metric filter reached output")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    result = engine.validate(query)
    assert result["errors"][0]["code"] == "POLICY_DENIED"
    for operation in (engine.compile, engine.query):
        with pytest.raises(SemanticLayerError) as exc:
            operation(query)
        assert exc.value.code == "POLICY_DENIED"
    return result["policy_effects"][0]["violations"]


@pytest.mark.parametrize("constraint", ["allowed_metric_filter_entities", "allow_metric_filters"])
@pytest.mark.parametrize("spelling", sorted(FILTER_SPELLINGS))
def test_metric_filter_spellings_count_as_their_entities(config, monkeypatch, spelling, constraint):
    expression, spelled, others = FILTER_SPELLINGS[spelling]
    query = _customer_query(expression)
    assert compiler.compile_query(config, None, query)["sql"]
    allowed = sorted([CUSTOMER, *others])
    engine = _constrained(
        config,
        {constraint: False} if constraint == "allow_metric_filters" else {constraint: allowed},
    )
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    if constraint == "allow_metric_filters":
        assert [row["kind"] for row in violations] == ["metric_filters_not_allowed"]
    else:
        assert violations == [
            {"kind": "disallowed_metric_filter_entity", "disallowed": [spelled], "allowed": allowed}
        ]


@pytest.mark.parametrize("spelling", sorted(FILTER_SPELLINGS))
def test_metric_filter_spellings_pass_when_their_entities_are_allowed(config, spelling):
    expression, spelled, others = FILTER_SPELLINGS[spelling]
    query = _customer_query(expression)
    engine = _constrained(config, {"allowed_metric_filter_entities": [CUSTOMER, spelled, *others]})
    try:
        result = engine.validate(query)
        assert result["ok"], result["errors"]
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


@pytest.mark.parametrize("spelling", sorted(CUSTOMER_FILTERS))
def test_metric_filters_on_allowed_entities_compile(config, spelling):
    query = _customer_query(CUSTOMER_FILTERS[spelling])
    engine = _constrained(config, {"allowed_metric_filter_entities": [CUSTOMER]})
    try:
        result = engine.validate(query)
        assert result["ok"], result["errors"]
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


@pytest.mark.parametrize("timed", [False, True])
def test_metric_filter_entities_exclude_the_constrained_cut(config, timed):
    """The select, grouping and time axis are governed by their own constraint keys."""
    query = {
        "select": [{"expression": {"measure": MEASURE}, "as": "revenue"}],
        "group_by": ["dimension.jaffle_store_name"],
        "metric_filters": [
            {
                "expression": {**CUSTOMER_FILTERS["predicate"], "scope_mode": "entity_only"},
                "op": "=",
                "value": True,
            }
        ],
    }
    if timed:
        query["time"] = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}
    bound = compiler.bind_query(config, None, query).object_ids
    assert {ENTITY, STORE} <= bound
    engine = _constrained(config, {"allowed_metric_filter_entities": [CUSTOMER]}, MEASURE)
    try:
        result = engine.validate(query)
        assert result["ok"], result["errors"]
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


@pytest.mark.parametrize(
    "constraint",
    [
        {"allowed_metric_filter_entities": [ENTITY, CUSTOMER]},
        {"allowed_metric_filter_metrics": [METRIC]},
        {"allow_metric_filters": False},
    ],
    ids=["entities", "metrics", "none"],
)
def test_literal_metric_filters_resolve_without_object_reads(config, monkeypatch, constraint):
    query = {
        "select": [{"expression": {"measure": MEASURE}, "as": "revenue"}],
        "metric_filters": [{"expression": {"kind": "literal", "value": 1}, "op": "=", "value": 1}],
    }
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(config, constraint, MEASURE)
    try:
        if "allow_metric_filters" in constraint:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations == [{"kind": "metric_filters_not_allowed", "metric_filter_refs": {}}]
        else:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
def test_metric_filter_entities_follow_package_entity_ids(tmp_path, monkeypatch, allowed):
    """Entity identity comes from the package, whatever the declared ids look like."""
    package = tmp_path / "jaffle_shop"
    shutil.copytree("configs/semantic_rails/jaffle_shop", package)
    graph = package / "graph.yml"
    graph.write_text(
        graph.read_text().replace(
            "    order:\n      label: Order\n",
            "    order:\n      id: orders\n      label: Order\n",
            1,
        )
    )
    config = replace(load_package_config(str(package)), semantic_policies=[])
    assert "orders" in {row.id for row in config.entities}
    query = _customer_query(FILTER_SPELLINGS["table_column"][0])
    policy = SemanticPolicyConfig(
        id="policy.test.filter_entities",
        kind="metric_constraint",
        object_ids=[CUSTOMER_COUNT],
        config={"allowed_metric_filter_entities": [CUSTOMER, *(["orders"] if allowed else [])]},
    )
    engine = Runtime.from_config(
        replace(config, semantic_policies=[policy]), source_path=str(package)
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == ["orders"]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("placement", ["aggregate", "nested", "scoped"])
def test_embedded_cuts_use_bound_entities(config, monkeypatch, allowed, placement):
    where = {"field": "dimension.jaffle_customer_type", "op": "=", "value": "returning"}
    expression = {"kind": "aggregate", "measure": MEASURE, "filter": {"all": [where]}}
    if placement == "scoped":
        expression = {"kind": "scoped_aggregate", "measure": MEASURE, "where": [where]}
    elif placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": {"measure": MEASURE}}
    query = {"select": [{"expression": expression, "as": "value"}]}
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(
        config, {"allowed_metric_filter_entities": [CUSTOMER] if allowed else []}, MEASURE
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == [CUSTOMER]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("spelling", ["override", "explicit"])
def test_effective_ordering_role_constrained_without_time(config, monkeypatch, allowed, spelling):
    measure_id = "measure.jaffle.delivered_revenue_usd"
    delivered = "temporal_role.jaffle_lifecycle_delivered_at"
    prepared = "temporal_role.jaffle_lifecycle_prepared_at"
    config = replace(
        config,
        measures=[
            replace(
                m,
                compatible_temporal_roles=[delivered, prepared],
                allowed_aggregations=[*m.allowed_aggregations, "last_value"],
            )
            if m.id == measure_id
            else m
            for m in config.measures
        ],
    )
    expression = {"measure": measure_id, "aggregation": "last_value"}
    query = {"select": [{"expression": expression, "as": "value"}]}
    if spelling == "override":
        query["temporal_role_overrides"] = {measure_id: prepared}
    else:
        expression["temporal_role"] = prepared
    assert "prepared_at" in compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(
        config,
        {"allowed_temporal_roles": [delivered, prepared] if allowed else [delivered]},
        measure_id,
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["temporal_role"] == prepared
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
def test_contextual_predicate_records_actual_clock_entity(config, monkeypatch, allowed):
    store_role = "temporal_role.jaffle_store_opened_at"
    customer_role = "temporal_role.jaffle_customer_first_order_at"
    config = replace(
        config,
        measures=[
            replace(
                m,
                compatible_temporal_roles=[store_role, customer_role],
                default_temporal_role=store_role,
            )
            if m.id == "measure.jaffle.order_count"
            else m
            for m in config.measures
        ],
    )
    query = {
        "select": [{"expression": {"measure": MEASURE}, "as": "revenue"}],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": ENTITY,
                    "input": {"measure": "measure.jaffle.order_count"},
                    "op": ">",
                    "value": 1,
                },
                "op": "=",
                "value": True,
            }
        ],
    }
    assert "jaffle_customer" in compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(
        config,
        {"allowed_metric_filter_entities": [ENTITY, STORE, *([CUSTOMER] if allowed else [])]},
        MEASURE,
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert CUSTOMER in violations[0]["disallowed"]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_cut_metric_allowlists_count_direct_and_nested_recipes(
    config, monkeypatch, allowed, nested
):
    from semantic_rails.schema import MetricConfig

    outer = "custom_outer_recipe"
    config = replace(
        config,
        metric_recipes=[
            *config.metric_recipes,
            MetricConfig(
                id=outer,
                kind="derived",
                expression=expressions.parse_semantic_expression(
                    {
                        "kind": "ratio",
                        "numerator": {"metric": METRIC},
                        "denominator": {"measure": "measure.jaffle.order_count"},
                    },
                    context="query",
                ),
            ),
        ],
    )
    query = _customer_query({"metric": outer if nested else METRIC})
    assert METRIC not in compiler.bind_query(config, None, {"select": query["select"]}).object_ids
    permitted = ([outer] if nested else []) + ([METRIC] if allowed else [])
    engine = _constrained(config, {"allowed_metric_filter_metrics": permitted})
    try:
        if allowed:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == [METRIC]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize(
    "kind,recipe",
    [
        (kind, recipe)
        for kind in ("aggregate", "scoped", "conditional", "predicate")
        for recipe in (False, True)
        if (kind, recipe) != ("conditional", True)
    ],
)
def test_embedded_cuts_obey_allow_false(config, monkeypatch, allowed, recipe, kind):
    from semantic_rails.schema import MetricConfig

    clause = {"field": "dimension.jaffle_customer_type", "op": "=", "value": "returning"}
    expr = {"kind": "aggregate", "measure": MEASURE, "filter": {"all": [clause]}}
    if kind == "scoped":
        expr = {"kind": "scoped_aggregate", "measure": MEASURE, "where": [clause]}
    elif kind == "conditional":
        expr = CUSTOMER_FILTERS["table_column"]
    elif kind == "predicate":
        expr = {
            "kind": "scoped_aggregate",
            "measure": MEASURE,
            "predicates": [
                {
                    "entity": CUSTOMER,
                    "input": {"measure": "measure.jaffle.lifetime_spend_usd"},
                    "op": ">",
                    "value": 100,
                }
            ],
        }
    if recipe:
        config = replace(
            config,
            metric_recipes=[
                *config.metric_recipes,
                MetricConfig(
                    id="cut_recipe",
                    kind="derived",
                    expression=expressions.parse_semantic_expression(expr, context="query"),
                ),
            ],
        )
        expr = {"metric": "cut_recipe"}
    query = {
        "select": [
            {"expression": {"measure": MEASURE}, "as": "revenue"},
            {"expression": expr, "as": "cut"},
        ]
    }
    assert compiler.compile_query(config, None, query)["sql"]
    # A conditional aggregate computes a synthetic measure, so a package-wide
    # policy governs its cut; the others cut the governed measure itself.
    governed = None if kind == "conditional" else MEASURE
    engine = _constrained(config, {"allow_metric_filters": allowed}, governed)
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["kind"] == "metric_filters_not_allowed"
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("placement", ["nested", "cut", "recipe", "recipe_policy"])
@pytest.mark.parametrize("shape", ["measure", "aggregate", "scoped_aggregate"])
def test_effective_temporal_roles_follow_governed_leaves(
    config, monkeypatch, allowed, placement, shape
):
    from semantic_rails.schema import MetricConfig

    measure_id = "measure.jaffle.delivered_revenue_usd"
    delivered = "temporal_role.jaffle_lifecycle_delivered_at"
    prepared = "temporal_role.jaffle_lifecycle_prepared_at"
    config = replace(
        config,
        measures=[
            replace(
                m,
                compatible_temporal_roles=[delivered, prepared],
                allowed_aggregations=[*m.allowed_aggregations, "last_value"],
            )
            if m.id == measure_id
            else m
            for m in config.measures
        ],
    )
    expr = {
        "kind": shape,
        "measure": measure_id,
        "aggregation": "last_value",
        "temporal_role": prepared,
    }
    governed = measure_id
    if placement in {"recipe", "recipe_policy"}:
        config = replace(
            config,
            metric_recipes=[
                *config.metric_recipes,
                MetricConfig(
                    id="pinned_recipe",
                    kind="derived",
                    expression=expressions.parse_semantic_expression(expr, context="query"),
                ),
            ],
        )
        expr = {"metric": "pinned_recipe"}
        if placement == "recipe_policy":
            governed = "pinned_recipe"
    elif placement == "nested":
        expr = {"kind": "call", "name": "COALESCE", "args": [expr, {"kind": "literal", "value": 0}]}
    query = {"select": [{"expression": expr, "as": "value"}]}
    if placement == "cut":
        query = {
            "select": [{"expression": {"measure": CUSTOMER_COUNT}, "as": "customers"}],
            "metric_filters": [{"expression": expr, "op": ">", "value": 1}],
        }
    assert "prepared_at" in compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(
        config,
        {"allowed_temporal_roles": [delivered, prepared] if allowed else [delivered]},
        governed,
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["temporal_role"] == prepared
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
def test_foreign_query_clock_uses_leaf_effective_role(config, monkeypatch, allowed):
    measure_id = "measure.jaffle.delivered_revenue_usd"
    delivered = "temporal_role.jaffle_lifecycle_delivered_at"
    prepared = "temporal_role.jaffle_lifecycle_prepared_at"
    query_role = "temporal_role.jaffle_customer_first_order_at"
    config = replace(
        config,
        measures=[
            replace(m, compatible_temporal_roles=[delivered, prepared]) if m.id == measure_id else m
            for m in config.measures
        ],
    )
    query = {
        "select": [
            {"expression": {"measure": measure_id, "temporal_role": prepared}, "as": "value"},
            {"expression": {"measure": CUSTOMER_COUNT}, "as": "customers"},
        ],
        "time": {"temporal_role": query_role, "grain": "month"},
    }
    assert "prepared_at" in compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(
        config,
        {"allowed_temporal_roles": [query_role, *([prepared] if allowed else [])]},
        measure_id,
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert prepared in [item["temporal_role"] for item in violations]
    finally:
        engine.close()


def test_aligned_window_filter_binds_in_real_context(config):
    query = {
        "select": [{"expression": {"measure": CUSTOMER_COUNT}, "as": "customers"}],
        "time": {"temporal_role": "temporal_role.jaffle_customer_first_order_at", "grain": "month"},
        "metric_filters": [
            {
                "expression": {
                    "kind": "cumulative",
                    "input": {"measure": "measure.jaffle.order_count"},
                },
                "op": ">",
                "value": 0,
            }
        ],
    }
    engine = _constrained(
        config, {"allowed_metric_filter_entities": [row.id for row in config.entities]}
    )
    try:
        result = engine.validate(query)
        assert result["ok"], result["errors"]
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


def test_cut_entities_exclude_attachment_path(config):
    item = "entity.jaffle_item"
    query = _customer_query(
        _count_where_not_zero({"kind": "column", "entity": item, "column": "item_id"})
    )
    bound = compiler.bind_query(config, None, query)
    assert ENTITY in bound.object_ids
    assert ENTITY not in set().union(*bound.cuts)
    engine = _constrained(config, {"allowed_metric_filter_entities": [CUSTOMER, item]})
    try:
        assert engine.validate(query)["ok"]
    finally:
        engine.close()


def test_shipped_sales_policy_applies_to_select_cuts(monkeypatch):
    config = load_package_config("configs/semantic_rails/jaffle_shop")
    query = {
        "select": [
            {
                "expression": {
                    "kind": "aggregate",
                    "measure": MEASURE,
                    "filter": {
                        "all": [
                            {
                                "field": "dimension.jaffle_customer_type",
                                "op": "=",
                                "value": "returning",
                            }
                        ]
                    },
                },
                "as": "revenue",
            }
        ],
        "policy_context": {"roles": ["sales"]},
    }
    engine = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        # The same shape is permitted for an unscoped caller.
        assert engine.validate({**query, "policy_context": {"roles": ["finance"]}})["ok"]
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        assert any(row["kind"] == "metric_filters_not_allowed" for row in violations)
    finally:
        engine.close()


STORE_CUT = {
    "kind": "aggregate",
    "measure": "measure.jaffle.order_count",
    "filter": {"all": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}]},
}
SIBLING_SHAPES = {
    # Cuts of leaves that do not compute the governed measure.
    "aggregate": STORE_CUT,
    "conditional": _count_where_not_zero(
        {"kind": "column", "table": "jaffle_customer", "column": "customer_id"}
    ),
    "recipe": {"metric": "store_share_recipe"},
    # The governed measure is read inside the sibling's cut.
    "predicate_input": {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.order_count",
        "predicates": [
            {"entity": CUSTOMER, "input": {"measure": MEASURE}, "op": ">", "value": 100}
        ],
    },
    # Lowered without a single owning leaf.
    "distribution": {
        "kind": "distribution",
        "function": "avg",
        "over": {
            "kind": "entity_value",
            "entity": CUSTOMER,
            "input": {"measure": "measure.jaffle.order_count"},
            "where": [{"kind": "value_filter", "op": ">", "value": 0}],
        },
    },
}


@pytest.mark.parametrize("shape", [*SIBLING_SHAPES, "metric_filter", "shared_scan"])
def test_shipped_sales_policy_scopes_cuts_to_governed_objects(monkeypatch, shape):
    """A cut counts for the leaves it filters; query-wide and unattributed cuts count for all."""
    from semantic_rails.compiler_parts import sql_lowering
    from semantic_rails.schema import MetricConfig

    config = load_package_config("configs/semantic_rails/jaffle_shop")
    recipe = {
        "kind": "ratio",
        "numerator": STORE_CUT,
        "denominator": {"measure": "measure.jaffle.order_count"},
    }
    config = replace(
        config,
        metric_recipes=[
            *config.metric_recipes,
            MetricConfig(
                id="store_share_recipe",
                kind="derived",
                expression=expressions.parse_semantic_expression(recipe, context="query"),
            ),
        ],
    )
    governed = {"expression": {"measure": MEASURE}, "as": "revenue"}
    query = {"select": [governed], "policy_context": {"roles": ["sales"]}}
    if shape == "metric_filter":
        query["metric_filters"] = [{"expression": STORE_CUT, "op": ">", "value": 0}]
    elif shape == "shared_scan":
        # Both filtered leaves read one scan; the filter is lowered for the first.
        store_filter = STORE_CUT["filter"]
        governed["expression"] = {"kind": "aggregate", "measure": MEASURE, "filter": store_filter}
        query["select"] = [{"expression": STORE_CUT, "as": "orders"}, governed]
        bound = compiler.bind_query(config, None, query)
        assert len(sql_lowering._measure_plan_groups(bound.plan, bound.config)) == 1
    else:
        query["select"].append({"expression": SIBLING_SHAPES[shape], "as": "sibling"})
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        if shape in {"aggregate", "conditional", "recipe"}:
            result = engine.validate(query)
            assert result["ok"], result["errors"]
            assert engine.compile(query)["rendered_sql"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert any(row["kind"] == "metric_filters_not_allowed" for row in violations)
    finally:
        engine.close()


@pytest.mark.parametrize("governed", [MEASURE, None])
def test_shared_scan_conditional_cut_counts_for_its_own_leaf(config, monkeypatch, governed):
    from semantic_rails.compiler_parts import sql_lowering

    config = replace(
        config,
        measures=[
            replace(m, source_relation="jaffle_order") if m.id == MEASURE else m
            for m in config.measures
        ],
    )
    query = {
        "select": [
            {"expression": {"measure": MEASURE}, "as": "revenue"},
            {"expression": FILTER_SPELLINGS["table_column"][0], "as": "orders"},
        ]
    }
    bound = compiler.bind_query(config, None, query)
    assert len(sql_lowering._measure_plan_groups(bound.plan, bound.config)) == 1
    engine = _constrained(config, {"allow_metric_filters": False}, governed)
    try:
        if governed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["kind"] == "metric_filters_not_allowed"
    finally:
        engine.close()


def test_object_cuts_fall_back_to_the_whole_query(config):
    sibling = {"select": [{"expression": {"measure": MEASURE}, "as": "revenue"}]}
    sibling["select"].append({"expression": STORE_CUT, "as": "orders"})
    bound = compiler.bind_query(config, None, sibling)
    assert bound.cuts and bound.object_cuts(MEASURE) == ()
    for object_id in ("measure.jaffle.order_count", STORE, ENTITY, CUSTOMER_COUNT):
        assert bound.object_cuts(object_id) == bound.cuts
    read_in_cut = {"select": [sibling["select"][0]]}
    read_in_cut["select"].append({"expression": SIBLING_SHAPES["predicate_input"], "as": "orders"})
    bound = compiler.bind_query(config, None, read_in_cut)
    assert bound.object_cuts(MEASURE) == bound.cuts


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("constraint", ["allowed_metric_filter_entities", "allow_metric_filters"])
def test_optimized_anchored_predicates_are_cuts(config, monkeypatch, allowed, constraint):
    measure_id = "measure.jaffle.inventory_on_hand_eop"
    aggregate = {"kind": "scoped_aggregate", "measure": measure_id, "aggregation": "sum"}
    numerator = {
        **aggregate,
        "predicates": [
            {"measure": "measure.jaffle.order_count", "entity": STORE, "op": ">", "value": 0}
        ],
    }
    query = {
        "version": 2,
        "select": [
            {
                "expression": {"kind": "ratio", "numerator": numerator, "denominator": aggregate},
                "as": "ratio",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
    }
    compiled = compiler.compile_query(config, None, query)
    assert any(
        row["kind"] == "anchored_entity_set" for row in compiled["physical_plan"].optimizations
    )
    policy = {
        constraint: allowed
        if constraint == "allow_metric_filters"
        else [STORE, *([ENTITY] if allowed else [])]
    }
    engine = _constrained(config, policy, measure_id)
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["kind"] in {
                "metric_filters_not_allowed",
                "disallowed_metric_filter_entity",
            }
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
def test_conversion_cut_records_operand_entities(config, monkeypatch, allowed):
    expression = {
        "kind": "conversion",
        "base": {"measure": "measure.jaffle.session_starts"},
        "converted": {"measure": "measure.jaffle.order_count"},
        "entity": CUSTOMER,
        "window": {"unit": "day", "value": 28},
        "matching_mode": "first_converted_after_base",
    }
    query = _customer_query(expression)
    query.pop("group_by")
    assert compiler.compile_query(config, None, query)["sql"]
    entities = [row.id for row in config.entities if allowed or row.id != ENTITY]
    engine = _constrained(config, {"allowed_metric_filter_entities": entities})
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == [ENTITY]
    finally:
        engine.close()


def test_timeless_count_does_not_consume_default_role(config):
    query = _customer_query({"measure": "measure.jaffle.item_count"})
    query.pop("group_by")
    engine = _constrained(config, {"allowed_metric_filter_entities": ["entity.jaffle_item"]})
    try:
        assert engine.validate(query)["ok"]
    finally:
        engine.close()


def test_missing_compiled_leaf_binding_is_refused(config, monkeypatch):
    from semantic_rails.compiler_parts import dependencies

    original = dependencies.record_leaf_reference

    def lose_leaf(alias):
        plan = dependencies._plan.get()
        if plan is not None:
            plan.leaves.pop(alias, None)
        original(alias)

    from semantic_rails.compiler_parts import post_aggregation

    monkeypatch.setattr(post_aggregation, "record_leaf_reference", lose_leaf)
    query = _customer_query({"measure": "measure.jaffle.order_count"})
    engine = _constrained(
        config, {"allowed_metric_filter_entities": [row.id for row in config.entities]}
    )
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        assert violations[0]["kind"] == "unresolved_metric_filter"
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("shape", ["conversion", "anchored"])
def test_recipe_roles_follow_optimized_operand_bindings(config, monkeypatch, allowed, shape):
    from semantic_rails.schema import MetricConfig

    measure_id = (
        "measure.jaffle.session_starts"
        if shape == "conversion"
        else "measure.jaffle.inventory_on_hand_eop"
    )
    measure = next(m for m in config.measures if m.id == measure_id)
    role = next(r for r in config.temporal_roles if r.id == measure.compatible_temporal_roles[0])
    dimension = next(d for d in config.dimensions if d.id == role.dimension)
    other_dimension = replace(dimension, id="dimension.test.other_clock", column="other_clock")
    other_role = replace(role, id="temporal_role.test.other_clock", dimension=other_dimension.id)
    measure = replace(measure, compatible_temporal_roles=[role.id, other_role.id])
    expression = {
        "kind": "scoped_aggregate" if shape == "anchored" else "measure",
        "measure": measure_id,
        "temporal_role": other_role.id,
    }
    if shape == "anchored":
        expression["aggregation"] = "sum"
        expression["predicates"] = [
            {"measure": "measure.jaffle.order_count", "entity": STORE, "op": ">", "value": 0}
        ]
    recipe = MetricConfig(
        id="operand_recipe",
        kind="derived",
        expression=expressions.parse_semantic_expression(expression, context="query"),
        compatible_temporal_roles=[role.id, other_role.id],
    )
    config = replace(
        config,
        measures=[measure if m.id == measure_id else m for m in config.measures],
        dimensions=[*config.dimensions, other_dimension],
        temporal_roles=[*config.temporal_roles, other_role],
        metric_recipes=[*config.metric_recipes, recipe],
    )
    if shape == "conversion":
        expr = {
            "kind": "conversion",
            "base": {"metric": recipe.id},
            "converted": {"measure": "measure.jaffle.order_count"},
            "entity": CUSTOMER,
            "window": {"unit": "day", "value": 28},
            "matching_mode": "first_converted_after_base",
        }
        query = {"select": [{"expression": expr, "as": "value"}]}
    else:
        denominator = {key: value for key, value in expression.items() if key != "predicates"}
        expr = {"kind": "ratio", "numerator": {"metric": recipe.id}, "denominator": denominator}
        query = {
            "select": [{"expression": expr, "as": "value"}],
            "time": {"temporal_role": role.id, "grain": "month"},
        }
    compiled = compiler.compile_query(config, None, query)
    assert "other_clock" in compiled["sql"]
    if shape == "anchored":
        assert any(
            row["kind"] == "anchored_entity_set" for row in compiled["physical_plan"].optimizations
        )
    engine = _constrained(
        config,
        {
            "allowed_temporal_roles": [
                role.id,
                # The anchored recipe also owns its order-count predicate's bucket.
                *(["temporal_role.jaffle_order_time"] if shape == "anchored" else []),
                *([other_role.id] if allowed else []),
            ]
        },
        recipe.id,
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["temporal_role"] == other_role.id
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("constraint", ["entities", "metrics"])
@pytest.mark.parametrize("optimized", [False, True])
def test_optimized_projection_remains_a_parent_predicate_cut(
    config, monkeypatch, allowed, constraint, optimized
):
    from semantic_rails.compiler_parts import sql_lowering
    from semantic_rails.schema import MetricConfig

    inventory = "entity.jaffle_store_inventory_snapshot"
    aggregate = {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.inventory_on_hand_eop",
        "aggregation": "sum",
    }
    numerator = {
        **aggregate,
        "predicates": [
            {"measure": "measure.jaffle.order_count", "entity": STORE, "op": ">", "value": 0}
        ],
    }
    recipe = MetricConfig(
        id="metric.test.inventory",
        kind="derived",
        expression=expressions.parse_semantic_expression(numerator, context="query"),
    )
    config = replace(config, metric_recipes=[*config.metric_recipes, recipe])
    expression = {
        "kind": "ratio",
        "numerator": {"metric": recipe.id} if constraint == "metrics" else numerator,
        "denominator": aggregate,
    }
    query = {
        "select": [{"expression": {"measure": MEASURE}, "as": "revenue"}],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": STORE,
                    "input": expression,
                    "op": ">",
                    "value": 0,
                },
                "op": "=",
                "value": True,
            }
        ],
    }
    original = sql_lowering._anchored_entity_set_select
    accepted = []

    def track_optimization(plan, package):
        result = original(plan, package) if optimized else None
        if result is not None:
            accepted.append(True)
        return result

    monkeypatch.setattr(sql_lowering, "_anchored_entity_set_select", track_optimization)
    assert "jaffle_store_inventory_snapshot" in compiler.compile_query(config, None, query)["sql"]
    assert bool(accepted) == optimized
    policy = (
        {"allowed_metric_filter_entities": [ENTITY, STORE, *([inventory] if allowed else [])]}
        if constraint == "entities"
        else {"allowed_metric_filter_metrics": [recipe.id] if allowed else []}
    )
    engine = _constrained(config, policy, MEASURE)
    try:
        if allowed:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == [
                inventory if constraint == "entities" else recipe.id
            ]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("outer_group", [False, True])
def test_distribution_cut_binds_its_own_entity_grain(config, monkeypatch, allowed, outer_group):
    query = {
        "select": [
            {
                "as": "distribution",
                "expression": {
                    "kind": "distribution",
                    "function": "avg",
                    "over": {
                        "kind": "entity_value",
                        "entity": CUSTOMER,
                        "input": {"measure": "measure.jaffle.order_count"},
                        "where": [{"kind": "value_filter", "op": ">", "value": 0}],
                    },
                },
            }
        ]
    }
    if outer_group:
        query["group_by"] = ["dimension.jaffle_store_name"]
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(
        config,
        {"allowed_metric_filter_entities": [ENTITY, *([CUSTOMER] if allowed else [])]},
        "measure.jaffle.order_count",
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == [CUSTOMER]
    finally:
        engine.close()


def test_distribution_cut_excludes_attachment_entities(config):
    item = "entity.jaffle_item"
    query = {
        "select": [
            {
                "as": "distribution",
                "expression": {
                    "kind": "distribution",
                    "function": "avg",
                    "over": {
                        "kind": "entity_value",
                        "entity": CUSTOMER,
                        "input": {"measure": "measure.jaffle.item_count"},
                        "where": [{"kind": "value_filter", "op": ">", "value": 0}],
                    },
                },
            }
        ]
    }
    bound = compiler.bind_query(config, None, query)
    cut_ids = set().union(*bound.cuts)
    assert {CUSTOMER, "dimension.jaffle_customer_id", item} <= cut_ids
    assert ENTITY in bound.object_ids and ENTITY not in cut_ids
    engine = _constrained(
        config, {"allowed_metric_filter_entities": [CUSTOMER, item]}, "measure.jaffle.item_count"
    )
    try:
        assert engine.validate(query)["ok"]
    finally:
        engine.close()


def test_unfiltered_distribution_has_no_cut(config):
    query = {
        "select": [
            {
                "as": "distribution",
                "expression": {
                    "kind": "distribution",
                    "function": "avg",
                    "over": {
                        "kind": "entity_value",
                        "entity": CUSTOMER,
                        "input": {"measure": "measure.jaffle.order_count"},
                    },
                },
            }
        ]
    }
    assert not compiler.bind_query(config, None, query).cuts
    engine = _constrained(config, {"allow_metric_filters": False}, "measure.jaffle.order_count")
    try:
        assert engine.validate(query)["ok"]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("own_filter", [False, True])
def test_distribution_projection_inherits_parent_cut(config, monkeypatch, allowed, own_filter):
    over = {
        "kind": "entity_value",
        "entity": CUSTOMER,
        "input": {"measure": "measure.jaffle.order_count"},
    }
    if own_filter:
        over["where"] = [{"kind": "value_filter", "op": ">", "value": 0}]
    expression = {"kind": "distribution", "function": "avg", "over": over}
    query = {
        "select": [{"expression": {"measure": MEASURE}, "as": "revenue"}],
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": STORE,
                    "input": expression,
                    "op": ">",
                    "value": 0,
                },
                "op": "=",
                "value": True,
            }
        ],
    }
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(
        config,
        {"allowed_metric_filter_entities": [ENTITY, STORE, *([CUSTOMER] if allowed else [])]},
        MEASURE,
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == [CUSTOMER]
    finally:
        engine.close()


@pytest.fixture
def recipe_predicate_config(config):
    measure_id = "measure.jaffle.delivered_revenue_usd"
    return replace(
        config,
        measures=[
            replace(
                measure,
                compatible_temporal_roles=[
                    "temporal_role.jaffle_lifecycle_delivered_at",
                    "temporal_role.jaffle_lifecycle_prepared_at",
                ],
                allowed_aggregations=[*measure.allowed_aggregations, "last_value"],
            )
            if measure.id == measure_id
            else measure
            for measure in config.measures
        ],
    )


def _recipe_with_predicate(config, role, *, nested=False, aggregate=False):
    from semantic_rails.schema import MetricConfig

    predicate = {
        "kind": "metric_predicate",
        "entity": CUSTOMER,
        "input": {
            "kind": "aggregate",
            "measure": "measure.jaffle.delivered_revenue_usd",
            "aggregation": "last_value",
            "temporal_role": role,
        },
        "op": ">",
        "value": 0,
    }
    expression = (
        {"kind": "aggregate", "measure": MEASURE, "filter": {"all": [{"expression": predicate}]}}
        if aggregate
        else {"kind": "scoped_aggregate", "measure": MEASURE, "predicates": [predicate]}
    )
    recipe = MetricConfig(
        id="metric.test.scoped_recipe",
        kind="derived",
        expression=expressions.parse_semantic_expression(expression, context="query"),
    )
    recipes = [*config.metric_recipes, recipe]
    if nested:
        recipe = MetricConfig(
            id="metric.test.outer_recipe",
            kind="derived",
            expression=expressions.parse_semantic_expression(
                {"metric": recipe.id}, context="query"
            ),
        )
        recipes.append(recipe)
    return replace(config, metric_recipes=recipes), recipe.id, predicate


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("aggregate", [False, True])
@pytest.mark.parametrize("control", ["denied", "allow_prepared", "use_delivered"])
def test_recipe_roles_include_owned_predicates(
    recipe_predicate_config, monkeypatch, nested, aggregate, control
):
    delivered = "temporal_role.jaffle_lifecycle_delivered_at"
    prepared = "temporal_role.jaffle_lifecycle_prepared_at"
    role = delivered if control == "use_delivered" else prepared
    config, recipe, _ = _recipe_with_predicate(
        recipe_predicate_config, role, nested=nested, aggregate=aggregate
    )
    query = {"select": [{"expression": {"metric": recipe}, "as": "value"}]}
    sql = compiler.compile_query(config, None, query)["sql"]
    assert (
        f"arg_max(jaffle_order_lifecycle.order_total_cents / 100.0, jaffle_order_lifecycle.{role.rsplit('_', 2)[-2]}_at)"
        in sql
    )
    engine = _constrained(
        config,
        {
            "allowed_temporal_roles": [
                delivered,
                *([prepared] if control == "allow_prepared" else []),
            ]
        },
        recipe,
    )
    try:
        if control == "denied":
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["temporal_role"] == prepared
        else:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        bound = compiler.bind_query(config, None, query)
        assert role in bound.temporal_roles[recipe]
        assert role in bound.temporal_roles["metric.test.scoped_recipe"]
    finally:
        engine.close()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("placement", ["sibling", "outer_predicate", "outer_time"])
def test_recipe_roles_exclude_unowned_predicates(recipe_predicate_config, nested, placement):
    delivered = "temporal_role.jaffle_lifecycle_delivered_at"
    prepared = "temporal_role.jaffle_lifecycle_prepared_at"
    config, recipe, predicate = _recipe_with_predicate(
        recipe_predicate_config, delivered, nested=nested
    )
    predicate = deepcopy(predicate)
    predicate["input"]["temporal_role"] = prepared
    query = {"select": [{"expression": {"metric": recipe}, "as": "value"}]}
    if placement == "sibling":
        query["select"].append(
            {
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": MEASURE,
                    "predicates": [predicate],
                },
                "as": "sibling",
            }
        )
    elif placement == "outer_predicate":
        query["metric_filters"] = [{"expression": predicate, "op": "=", "value": True}]
    else:
        query["time"] = {
            "temporal_role": "temporal_role.jaffle_inventory_day",
            "grain": "month",
        }
        query["group_by"] = ["dimension.jaffle_store_name"]
        query["select"].append(
            {"expression": {"measure": "measure.jaffle.inventory_on_hand_eop"}, "as": "inventory"}
        )
    expected_roles = {delivered}
    if placement == "outer_time":
        expected_roles.add("temporal_role.jaffle_order_time")
    sql = compiler.compile_query(config, None, query)["sql"]
    if placement == "outer_time":
        assert "temporal_role.jaffle_inventory_day" in sql
    else:
        assert "jaffle_order_lifecycle.prepared_at" in sql
    # The query axis has its own constraint check; it must not become a recipe dependency.
    allowed_roles = expected_roles | (
        {"temporal_role.jaffle_inventory_day"} if placement == "outer_time" else set()
    )
    engine = _constrained(config, {"allowed_temporal_roles": sorted(allowed_roles)}, recipe)
    try:
        assert engine.validate(query)["ok"]
        assert engine.compile(query)["rendered_sql"]
        bound = compiler.bind_query(config, None, query)
        assert set(bound.temporal_roles[recipe]) == expected_roles
    finally:
        engine.close()


@pytest.mark.parametrize("optimized", [False, True])
@pytest.mark.parametrize("governed_operand", ["numerator", "denominator"])
@pytest.mark.parametrize("allowed", [False, True])
def test_optimized_recipe_roles_keep_predicate_ownership(
    recipe_predicate_config, monkeypatch, optimized, governed_operand, allowed
):
    from semantic_rails.compiler_parts import sql_lowering
    from semantic_rails.schema import MetricConfig

    delivered = "temporal_role.jaffle_lifecycle_delivered_at"
    prepared = "temporal_role.jaffle_lifecycle_prepared_at"
    inventory = "temporal_role.jaffle_inventory_day"
    config, _, predicate = _recipe_with_predicate(recipe_predicate_config, delivered)
    predicate["entity"] = STORE
    extra = deepcopy(predicate)
    extra["input"]["temporal_role"] = prepared
    aggregate = {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.inventory_on_hand_eop",
        "aggregation": "sum",
    }
    recipes = [
        MetricConfig(
            id=f"metric.test.{operand}",
            kind="derived",
            expression=expressions.parse_semantic_expression(
                {**aggregate, "predicates": predicates}, context="query"
            ),
        )
        for operand, predicates in [("numerator", [predicate, extra]), ("denominator", [predicate])]
    ]
    config = replace(config, metric_recipes=[*config.metric_recipes, *recipes])
    query = {
        "select": [
            {
                "expression": {
                    "kind": "ratio",
                    "numerator": {"metric": recipes[0].id},
                    "denominator": {"metric": recipes[1].id},
                },
                "as": "value",
            }
        ],
        "time": {"temporal_role": inventory, "grain": "month"},
    }
    original = sql_lowering._anchored_entity_set_select
    accepted = []

    def track_optimization(plan, package):
        result = original(plan, package) if optimized else None
        if result is not None:
            accepted.append(True)
        return result

    monkeypatch.setattr(sql_lowering, "_anchored_entity_set_select", track_optimization)
    assert (
        "jaffle_order_lifecycle.prepared_at" in compiler.compile_query(config, None, query)["sql"]
    )
    assert bool(accepted) == optimized
    engine = _constrained(
        config,
        {"allowed_temporal_roles": [inventory, delivered, *([prepared] if allowed else [])]},
        f"metric.test.{governed_operand}",
    )
    try:
        if governed_operand == "numerator" and not allowed:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["temporal_role"] == prepared
        else:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        bound = compiler.bind_query(config, None, query)
        assert prepared in bound.temporal_roles[recipes[0].id]
        assert prepared not in bound.temporal_roles[recipes[1].id]
        assert delivered in bound.temporal_roles[recipes[1].id]
    finally:
        engine.close()


@pytest.mark.parametrize("allowed", [False, True])
def test_recipe_roles_follow_recursive_owned_predicates(
    recipe_predicate_config, monkeypatch, allowed
):
    from semantic_rails.schema import MetricConfig

    delivered = "temporal_role.jaffle_lifecycle_delivered_at"
    prepared = "temporal_role.jaffle_lifecycle_prepared_at"
    config, inner, _ = _recipe_with_predicate(recipe_predicate_config, prepared)
    outer = MetricConfig(
        id="metric.test.recursive_recipe",
        kind="derived",
        expression=expressions.parse_semantic_expression(
            {
                "kind": "scoped_aggregate",
                "measure": MEASURE,
                "predicates": [
                    {"entity": CUSTOMER, "input": {"metric": inner}, "op": ">", "value": 0}
                ],
            },
            context="query",
        ),
    )
    config = replace(config, metric_recipes=[*config.metric_recipes, outer])
    query = {"select": [{"expression": {"metric": outer.id}, "as": "value"}]}
    assert (
        "jaffle_order_lifecycle.prepared_at" in compiler.compile_query(config, None, query)["sql"]
    )
    engine = _constrained(
        config, {"allowed_temporal_roles": [delivered, *([prepared] if allowed else [])]}, outer.id
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["temporal_role"] == prepared
        bound = compiler.bind_query(config, None, query)
        assert prepared in bound.temporal_roles[inner]
        assert prepared in bound.temporal_roles[outer.id]
    finally:
        engine.close()


def _aggregate_if_nested_case(position, *, reference_in_else):
    column = {"kind": "column", "entity": ENTITY, "column": "order_id"}
    reference = (
        {"kind": "comparison", "op": ">", "left": column, "right": {"kind": "literal", "value": 0}}
        if position == "condition"
        else column
    )
    literal = {"kind": "literal", "value": True if position == "condition" else 7}
    case = {
        "kind": "case",
        "whens": [
            {
                "when": {"kind": "literal", "value": False},
                "then": literal if reference_in_else else reference,
            }
        ],
        "else": reference if reference_in_else else literal,
    }
    expression = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": {"kind": "literal", "value": True},
        "value": {"kind": "literal", "value": 7},
        position: case,
    }
    return {"select": [{"expression": expression, "as": "conditional_value"}]}


@pytest.mark.parametrize("position", ["condition", "value"])
@pytest.mark.parametrize("reference_in_else", [False, True])
def test_conditional_aggregate_preserves_nested_case_results(config, position, reference_in_else):
    import duckdb

    query = _aggregate_if_nested_case(position, reference_in_else=reference_in_else)
    sql = compiler.compile_query(config, None, query)["sql"]
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE jaffle_order(order_id INTEGER)")
        connection.execute("INSERT INTO jaffle_order VALUES (1), (2)")
        expected = 3 if position == "value" and reference_in_else else 14
        assert connection.execute(sql).fetchall() == [(expected,)]


@pytest.mark.parametrize("allowed", [False, True])
def test_conditional_aggregate_else_condition_is_a_cut(config, monkeypatch, allowed):
    query = _aggregate_if_nested_case("condition", reference_in_else=True)
    query["select"].append({"expression": {"measure": MEASURE}, "as": "revenue"})
    engine = _constrained(
        config, {"allowed_metric_filter_entities": [ENTITY] if allowed else []}, None
    )
    try:
        if allowed:
            assert engine.validate(query)["ok"]
            assert engine.compile(query)["rendered_sql"]
            assert any(ENTITY in cut for cut in compiler.bind_query(config, None, query).cuts)
        else:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations[0]["disallowed"] == [ENTITY]
    finally:
        engine.close()


@pytest.mark.parametrize("reference_in_else", [False, True])
def test_conditional_aggregate_nested_value_is_not_a_cut(config, reference_in_else):
    query = _aggregate_if_nested_case("value", reference_in_else=reference_in_else)
    query["select"][0]["expression"]["value"]["whens"][0]["when"] = {
        "kind": "comparison",
        "op": ">",
        "left": {"kind": "column", "entity": ENTITY, "column": "order_id"},
        "right": {"kind": "literal", "value": 0},
    }
    query["select"].append({"expression": {"measure": MEASURE}, "as": "revenue"})
    engine = _constrained(config, {"allowed_metric_filter_entities": []}, None)
    try:
        assert engine.validate(query)["ok"]
        assert engine.compile(query)["rendered_sql"]
        bound = compiler.bind_query(config, None, query)
        assert ENTITY in bound.object_ids
        assert all(ENTITY not in cut for cut in bound.cuts)
    finally:
        engine.close()
