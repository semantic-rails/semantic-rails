"""Column aggregates inherit constraints on measures reading the same source columns."""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace

import duckdb
import pytest

from semantic_rails import compiler
from semantic_rails.compiler_parts import bind
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import ColumnRefExpr
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config

PACKAGE = "configs/semantic_rails/jaffle_shop"
REVENUE = "measure.jaffle.revenue_usd"
ORDER = "entity.jaffle_order"
ORDER_COUNT = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
SUM_REVENUE = {
    "kind": "aggregate_if",
    "aggregation": "sum",
    "condition": {
        "kind": "comparison",
        "op": ">",
        "left": {"kind": "column", "entity": ORDER, "column": "order_total_cents"},
        "right": {"kind": "literal", "value": 0},
    },
    "value": {"kind": "column", "entity": ORDER, "column": "order_total_cents"},
}


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config(PACKAGE), semantic_policies=[])


def _policy(constraint, governed=REVENUE, **scope):
    return SemanticPolicyConfig(
        id="policy.test.column_constraints",
        kind="metric_constraint",
        object_ids=[governed],
        config=constraint,
        **scope,
    )


def _query(expression=SUM_REVENUE, **extra):
    return {"select": [{"expression": expression, "as": "value"}], **extra}


CONSTRAINTS = {
    "allowed_where": ({"allowed_where": [STORE]}, "disallowed_where", {}),
    "allow_metric_filters": ({"allow_metric_filters": False}, "metric_filters_not_allowed", {}),
    "required_where": ({"required_where": [STORE]}, "missing_required_where", {}),
    "required_group_by": ({"required_group_by": [STORE]}, "missing_required_group_by", {}),
    "allowed_group_by": ({"allowed_group_by": []}, "disallowed_group_by", {"group_by": [STORE]}),
    "allowed_metric_filter_entities": (
        {"allowed_metric_filter_entities": []},
        "disallowed_metric_filter_entity",
        {},
    ),
    "allowed_metric_filter_metrics": (
        {"allowed_metric_filter_metrics": []},
        "disallowed_metric_filter_metric",
        {
            "metric_filters": [
                {"expression": {"metric": "metric.sales.customer_count"}, "op": ">", "value": 0}
            ]
        },
    ),
}


def _denied(engine, monkeypatch, query, kind):
    def no_output(*args, **kwargs):
        pytest.fail("a forbidden column aggregate reached output")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    result = engine.validate(query)
    assert result["errors"][0]["code"] == "POLICY_DENIED"
    violations = [
        row for effect in result["policy_effects"] for row in effect.get("violations", [])
    ]
    assert kind in [row["kind"] for row in violations]
    for operation in (engine.compile, engine.query):
        with pytest.raises(SemanticLayerError) as raised:
            operation(query)
        assert raised.value.code == "POLICY_DENIED"
    return violations


@pytest.mark.parametrize("key", sorted(CONSTRAINTS))
def test_a_column_aggregate_cannot_bypass_a_measure_constraint(config, monkeypatch, key):
    constraint, kind, extra = CONSTRAINTS[key]
    query = _query(**extra)
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy(constraint)]), source_path=PACKAGE
    )
    try:
        _denied(engine, monkeypatch, query, kind)
    finally:
        engine.close()


@pytest.mark.parametrize("position", ["condition", "value"])
@pytest.mark.parametrize("qualifier", ["entity", "table", "table_uppercase", "entity_alias"])
def test_every_read_uses_its_physical_source_column(config, monkeypatch, position, qualifier):
    query = _query(deepcopy(SUM_REVENUE))
    expression = query["select"][0]["expression"]
    expression["condition"]["left"]["column"] = "order_cost_cents"
    expression["value"]["column"] = "order_cost_cents"
    ref = expression["value"] if position == "value" else expression["condition"]["left"]
    ref["column"] = "order_total_cents"
    if qualifier.startswith("table"):
        ref.pop("entity")
        ref["table"] = "jaffle_order"
        if qualifier == "table_uppercase":
            # The relation must resolve, but SQL engines can resolve column names case-insensitively.
            ref["column"] = "ORDER_TOTAL_CENTS"
    elif qualifier == "entity_alias":
        entity = next(row for row in config.entities if row.id == ORDER)
        alias = replace(entity, id="entity.test.order_alias")
        config = replace(config, entities=[*config.entities, alias])
        # Keep every read on the alias's grain; the governed measure uses the original entity.
        for node in (expression["condition"]["left"], expression["value"]):
            node["entity"] = alias.id
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy({"allow_metric_filters": False})]),
        source_path=PACKAGE,
    )
    try:
        _denied(engine, monkeypatch, query, "metric_filters_not_allowed")
    finally:
        engine.close()


@pytest.mark.parametrize("qualified", ["measure", "aggregate"])
@pytest.mark.parametrize(
    "relation",
    ["main.jaffle_order", '"main"."JAFFLE_ORDER"', "`main`.`jaffle_order`", "[main].[jaffle_order]"],
)
def test_relation_qualification_and_quoting_cannot_bypass_constraints(
    config, monkeypatch, qualified, relation
):
    expression = deepcopy(SUM_REVENUE)
    if qualified == "measure":
        config = replace(
            config,
            measures=[
                replace(row, source_relation=relation) if row.id == REVENUE else row
                for row in config.measures
            ],
        )
    else:
        entity = next(row for row in config.entities if row.id == ORDER)
        alias = replace(entity, id="entity.test.qualified_order", table=relation)
        config = replace(config, entities=[*config.entities, alias])
        for ref in (expression["condition"]["left"], expression["value"]):
            ref["entity"] = alias.id
    query = _query(expression)
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy({"allowed_where": [STORE]})]),
        source_path=PACKAGE,
    )
    try:
        _denied(engine, monkeypatch, query, "disallowed_where")
    finally:
        engine.close()


@pytest.mark.parametrize("constraint_key", ["allowed_where", "allow_metric_filters"])
@pytest.mark.parametrize("governed_aggregation", ["count", "count_distinct"])
@pytest.mark.parametrize("source_relation", ["", '"main"."JAFFLE_ORDER"'])
@pytest.mark.parametrize(
    ("aggregation", "value"),
    [("count", None), ("count", "customer_id"), ("count_distinct", "customer_id")],
)
def test_counts_inherit_count_measure_constraints_on_their_relation(
    config, monkeypatch, constraint_key, governed_aggregation, source_relation, aggregation, value
):
    config = replace(
        config,
        measures=[
            replace(row, default_aggregation=governed_aggregation, source_relation=source_relation)
            if row.id == ORDER_COUNT
            else row
            for row in config.measures
        ],
    )
    expression = deepcopy(SUM_REVENUE)
    expression["aggregation"] = aggregation
    expression["condition"]["left"]["column"] = "order_cost_cents"
    if value is None:
        expression.pop("value")
    else:
        expression["value"]["column"] = value
    constraint, kind, _ = CONSTRAINTS[constraint_key]
    query = _query(expression)
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy(constraint, ORDER_COUNT)]), source_path=PACKAGE
    )
    try:
        _denied(engine, monkeypatch, query, kind)
    finally:
        engine.close()


@pytest.mark.parametrize("placement", ["select", "ratio", "metric_filter", "predicate"])
def test_nested_conditional_aggregates_inherit_constraints(config, monkeypatch, placement):
    query = _query()
    if placement == "ratio":
        query = _query(
            {"kind": "ratio", "numerator": SUM_REVENUE, "denominator": {"measure": REVENUE}}
        )
    elif placement in {"metric_filter", "predicate"}:
        expr = SUM_REVENUE
        if placement == "predicate":
            expr = {
                "kind": "metric_predicate",
                "entity": ORDER,
                "input": expr,
                "op": ">",
                "value": 0,
            }
        query = _query(
            {"measure": "measure.jaffle.order_count"},
            metric_filters=[
                {
                    "expression": expr,
                    "op": "=" if placement == "predicate" else ">",
                    "value": True if placement == "predicate" else 0,
                }
            ],
        )
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy({"allowed_where": [STORE]})]),
        source_path=PACKAGE,
    )
    try:
        _denied(engine, monkeypatch, query, "disallowed_where")
    finally:
        engine.close()


def test_missing_compiler_cut_recording_never_admits_a_column_filter(config, monkeypatch):
    monkeypatch.setattr(bind, "binding_cut", nullcontext)
    assert not compiler.bind_query(config, None, _query()).cuts
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy({"allow_metric_filters": False})]),
        source_path=PACKAGE,
    )
    try:
        violations = _denied(engine, monkeypatch, _query(), "metric_filters_not_allowed")
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "metric_filters_not_allowed",
            "metric_filter_refs": {},
            "source": "inline_expression",
        }
    ]


def test_every_measure_sharing_the_column_must_satisfy_every_constraint(config, monkeypatch):
    revenue = next(row for row in config.measures if row.id == REVENUE)
    other = replace(revenue, id="measure.test.other_revenue")
    policies = [
        _policy({"allowed_metric_filter_entities": [ORDER]}),
        replace(_policy({"allow_metric_filters": False}), id="policy.test.second"),
        replace(_policy({"required_where": [STORE]}, other.id), id="policy.test.other"),
    ]
    engine = Runtime.from_config(
        replace(config, measures=[*config.measures, other], semantic_policies=policies),
        source_path=PACKAGE,
    )
    try:
        violations = _denied(engine, monkeypatch, _query(), "metric_filters_not_allowed")
    finally:
        engine.close()
    assert "missing_required_where" in [row["kind"] for row in violations]


@pytest.mark.parametrize("indirection", ["joined_column", "lookup"])
def test_columns_read_from_another_entity_inherit_constraints(config, monkeypatch, indirection):
    revenue = next(row for row in config.measures if row.id == REVENUE)
    customer = "entity.jaffle_customer"
    if indirection == "joined_column":
        governed = REVENUE
        revenue = replace(revenue, expr=ColumnRefExpr(entity=customer, column="lifetime_value"))
        config = replace(
            config, measures=[revenue if row.id == REVENUE else row for row in config.measures]
        )
        expression = deepcopy(SUM_REVENUE)
        for ref in (expression["condition"]["left"], expression["value"]):
            ref.update(entity=customer, column="lifetime_value")
    else:
        # A lookup's value comes from the parent measure; its expression is only its key.
        lookup = replace(
            revenue,
            id="measure.test.customer_revenue",
            entity=customer,
            source_relation="jaffle_customer",
            expr=ColumnRefExpr(column="customer_id"),
            lookup_from=REVENUE,
            lookup_via=customer,
        )
        config = replace(config, measures=[*config.measures, lookup])
        governed = lookup.id
        expression = SUM_REVENUE
    query = _query(expression)
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy({"allow_metric_filters": False}, governed)]),
        source_path=PACKAGE,
    )
    try:
        _denied(engine, monkeypatch, query, "metric_filters_not_allowed")
    finally:
        engine.close()


@pytest.mark.parametrize("source", [REVENUE, "measure.test.missing"])
def test_an_unresolvable_constrained_lookup_never_admits_a_raw_aggregate(
    config, monkeypatch, source
):
    revenue = next(row for row in config.measures if row.id == REVENUE)
    changed = replace(revenue, lookup_from=source)
    engine = Runtime.from_config(
        replace(
            config,
            measures=[changed if row.id == REVENUE else row for row in config.measures],
            semantic_policies=[_policy({"allow_metric_filters": False})],
        ),
        source_path=PACKAGE,
    )

    def no_output(*args, **kwargs):
        pytest.fail("an unresolved constrained source reached output")

    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    try:
        result = engine.validate(_query())
        assert result["errors"][0]["code"] == "INVALID_CONFIG"
        with pytest.raises(SemanticLayerError) as raised:
            engine.query(_query())
        assert raised.value.code == "INVALID_CONFIG"
    finally:
        engine.close()


@pytest.mark.parametrize("table", [None, "jaffle_order"])
@pytest.mark.parametrize("source_relation", ["jaffle_order", "other_orders"])
def test_measure_relation_overrides_match_the_relation_sql_actually_reads(
    config, table, source_relation
):
    revenue = next(row for row in config.measures if row.id == REVENUE)
    ref = ColumnRefExpr(column="order_total_cents", table=table or "")
    changed = replace(revenue, expr=ref, source_relation=source_relation)
    config = replace(
        config, measures=[changed if row.id == REVENUE else row for row in config.measures]
    )
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy({"allow_metric_filters": False})]),
        source_path=PACKAGE,
    )
    try:
        result = engine.validate(_query())
    finally:
        engine.close()
    denied = bool(table) or source_relation == "jaffle_order"
    assert result["ok"] is not denied, result
    if denied:
        assert result["errors"][0]["code"] == "POLICY_DENIED"


@pytest.mark.parametrize("scoped", ["audiences", "roles", "environments"])
@pytest.mark.parametrize("matches", [False, True])
def test_inherited_constraints_retain_their_request_scope(config, scoped, matches):
    value = "production" if scoped == "environments" else "finance"
    context_key = {"audiences": "audience", "environments": "environment", "roles": "roles"}[scoped]
    request_value = value if matches else "staging" if scoped == "environments" else "customer"
    context = {context_key: [request_value] if scoped == "roles" else request_value}
    engine = Runtime.from_config(
        replace(
            config,
            semantic_policies=[_policy({"allow_metric_filters": False}, **{scoped: [value]})],
        ),
        source_path=PACKAGE,
    )
    try:
        result = engine.validate(_query(policy_context=context))
    finally:
        engine.close()
    assert result["ok"] is not matches, result
    if matches:
        assert result["errors"][0]["code"] == "POLICY_DENIED"


def test_a_temporal_axis_on_a_column_aggregate_stays_refused(config):
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy({"allowed_temporal_roles": []})]),
        source_path=PACKAGE,
    )
    try:
        result = engine.validate(
            _query(time={"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"})
        )
    finally:
        engine.close()
    assert result["errors"][0]["code"] == "INCOMPATIBLE_TEMPORAL_ROLE"


@pytest.mark.parametrize("constrained", [False, True])
def test_allowed_and_unconstrained_columns_match_reference_sql(tmp_path, constrained):
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = replace(load_package_config(str(package)), semantic_policies=[])
    expression = deepcopy(SUM_REVENUE)
    if not constrained:
        # A different column on the same entity has no revenue constraint.
        expression["condition"]["left"]["column"] = "order_cost_cents"
        expression["value"]["column"] = "order_cost_cents"
    constraint = (
        {
            "required_group_by": [STORE],
            "allowed_group_by": [STORE],
            "required_where": [STORE],
            "allowed_metric_filter_entities": [ORDER],
            "allowed_metric_filter_metrics": [],
            "allowed_temporal_roles": [],
            "allow_metric_filters": True,
        }
        if constrained
        else {"allow_metric_filters": False, "allowed_where": []}
    )
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy(constraint)]), source_path=str(package)
    )
    query = _query(
        expression, group_by=[STORE], where=[{"field": STORE, "op": "=", "value": "Brooklyn"}]
    )
    try:
        actual = engine.query(query)["rows"]
        db_path = engine.db_path
    finally:
        engine.close()
    column = "order_total_cents" if constrained else "order_cost_cents"
    with duckdb.connect(db_path, read_only=True) as connection:
        expected = connection.execute(
            f"SELECT s.store_name, SUM(CASE WHEN o.{column} > 0 THEN o.{column} END) "
            "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name = 'Brooklyn' GROUP BY s.store_name"
        ).fetchall()
    assert len(actual) == len(expected) == 1
    assert actual[0][STORE] == expected[0][0]
    assert actual[0]["value"] == pytest.approx(expected[0][1])
