"""Adversarial calls against the model's allowed filters: each one tries to filter, or read
around a required filter, through a door the policy check must also guard."""

from dataclasses import replace

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

PACKAGE = "configs/semantic_rails/jaffle_shop"
REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
STORE_NAME = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
PRODUCT_TYPE = "dimension.jaffle_product_type"
CUSTOMER = "entity.jaffle_customer"
PHILADELPHIA = {"field": STORE_NAME, "op": "is", "value": "Philadelphia"}
RETURNING = {"field": CUSTOMER_TYPE, "op": "is", "value": "returning"}


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config(PACKAGE), semantic_policies=[])


def _engine(config, constraint=None, governed=REVENUE, **changes):
    policies = (
        [
            SemanticPolicyConfig(
                id="policy.test.filters",
                kind="metric_constraint",
                object_ids=[governed],
                config=constraint,
            )
        ]
        if constraint
        else []
    )
    return Runtime.from_config(
        replace(config, semantic_policies=policies, **changes), source_path=PACKAGE
    )


def _refusal(engine, query):
    """The first error of validate, after checking compile refuses with the same code."""
    report = engine.validate(query)
    assert report["ok"] is False, report
    error = report["errors"][0]
    with pytest.raises(SemanticLayerError) as raised:
        engine.compile(query)
    assert raised.value.code == error["code"]
    return error


def _violations(error):
    assert error["code"] == "POLICY_DENIED"
    return [row["kind"] for row in error["details"]["policy_violations"]]


# --- G1: a required filter is met only by the same word and values ------------------------


@pytest.mark.parametrize(
    "condition",
    [
        {"field": STORE_NAME, "op": "not", "value": "Brooklyn"},
        {"field": STORE_NAME, "op": "!=", "value": "Brooklyn"},
        {"field": STORE_NAME, "op": "contains", "value": "a"},
        {"field": STORE_NAME, "op": "gte", "value": "A"},
        {"field": STORE_NAME, "op": "missing", "value": False},
    ],
    ids=["not", "not-alias", "contains", "range", "missing"],
)
def test_a_required_filter_is_not_met_by_another_word(config, condition):
    engine = _engine(config, {"required_where": [STORE_NAME]})
    try:
        query = {"select": [REVENUE], "where": [condition]}
        assert "missing_required_where" in _violations(_refusal(engine, query))
        assert engine.validate({"select": [REVENUE], "where": [PHILADELPHIA]})["ok"]
    finally:
        engine.close()


@pytest.mark.parametrize(
    ("condition", "ok"),
    [
        (PHILADELPHIA, True),
        ({"field": STORE_NAME, "op": "=", "value": "Philadelphia"}, True),
        ({"field": STORE_NAME, "op": "is", "value": ["Philadelphia"]}, True),
        ({"field": STORE_NAME, "op": "is", "value": ["Philadelphia", "Brooklyn"]}, False),
        ({"field": STORE_NAME, "op": "is", "value": "Brooklyn"}, False),
        ({"field": STORE_NAME, "op": "not", "value": "Philadelphia"}, False),
    ],
)
def test_a_required_value_is_met_only_by_that_value(config, condition, ok):
    spec = {"field": STORE_NAME, "op": "=", "value": "Philadelphia"}
    engine = _engine(config, {"required_where": [spec]})
    try:
        query = {"select": [REVENUE], "where": [condition]}
        if ok:
            assert engine.validate(query)["ok"]
        else:
            assert "missing_required_where" in _violations(_refusal(engine, query))
    finally:
        engine.close()


# --- G2: an entity filter's aggregate escapes the query's where ----------------------------


def _entity_filter(over, field=REVENUE):
    return {
        "entity": CUSTOMER,
        "having": [{"field": field, "op": "gt", "value": 100}],
        "over": over,
    }


@pytest.mark.parametrize("over", ["all_time", "window", "period"])
def test_a_required_filter_is_not_bypassed_by_an_entity_filter(config, over):
    """Customers' revenue over all stores would leak through the threshold."""
    engine = _engine(config, {"required_where": [STORE_NAME]})
    query = {
        "select": [ORDERS],
        "where": [PHILADELPHIA, _entity_filter(over)],
        "observation_scope": "query",
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
            "start": "2016-09-01",
            "end": "2016-11-01",
        },
    }
    try:
        assert "required_where_outside_entity_filter" in _violations(_refusal(engine, query))
        # The same filter on an ungoverned measure, or no entity filter at all, answers.
        assert engine.validate({**query, "where": [PHILADELPHIA, _entity_filter(over, ORDERS)]})[
            "ok"
        ]
        assert engine.validate({**query, "select": [REVENUE], "where": [PHILADELPHIA]})["ok"]
    finally:
        engine.close()


@pytest.mark.parametrize("input_", [REVENUE, "metric.sales.aov_usd"])
def test_a_required_filter_is_not_bypassed_by_a_scoped_predicate(config, input_):
    """Through a scoped aggregate's predicate too, and through a metric that reads it."""
    engine = _engine(config, {"required_where": [STORE_NAME]})
    query = {
        "select": [
            {
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": ORDERS,
                    "predicates": [
                        {
                            "entity": CUSTOMER,
                            "input": input_,
                            "op": "gt",
                            "value": 0,
                            "over": "all_time",
                        }
                    ],
                },
                "as": "orders",
            }
        ],
        "where": [PHILADELPHIA],
        "observation_scope": "query",
    }
    try:
        assert "required_where_outside_entity_filter" in _violations(_refusal(engine, query))
    finally:
        engine.close()


# --- G3: only the filter words are operators -----------------------------------------------


BAD_OPS = ["+", "OR", "<=>", "||", "; DROP TABLE x"]


def _placements(op):
    bad = {"field": STORE_NAME, "op": op, "value": "Philadelphia"}
    return {
        "where": {"select": [REVENUE], "where": [bad]},
        "entity_any": {"select": [REVENUE], "where": [{"entity": CUSTOMER, "any": [bad]}]},
        "having": {
            "select": [REVENUE],
            "having": [{"field": "revenue_usd", "op": op, "value": 0}],
        },
        "aggregate_filter": {
            "select": [
                {
                    "expression": {
                        "kind": "aggregate",
                        "measure": REVENUE,
                        "filter": {"all": [bad]},
                    },
                    "as": "revenue",
                }
            ]
        },
        "scoped_where": {
            "select": [
                {
                    "expression": {"kind": "scoped_aggregate", "measure": REVENUE, "where": [bad]},
                    "as": "revenue",
                }
            ]
        },
        "entity_having": {
            "select": [ORDERS],
            "where": [
                {
                    "entity": CUSTOMER,
                    "having": [{"field": REVENUE, "op": op, "value": 1}],
                    "over": "all_time",
                }
            ],
        },
    }


@pytest.mark.parametrize("op", BAD_OPS)
@pytest.mark.parametrize("placement", sorted(_placements("+")))
def test_a_non_word_op_is_refused_everywhere(config, op, placement):
    engine = _engine(config)
    try:
        error = _refusal(engine, _placements(op)[placement])
    finally:
        engine.close()
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"].endswith(".op")
    assert error["recovery_hints"][0]["code"] == "USE_FILTER_WORD"


@pytest.mark.parametrize("op", BAD_OPS)
def test_a_non_word_op_is_refused_in_the_engine_form(config, op):
    engine = _engine(config)
    query = {
        "select": [{"expression": {"kind": "measure", "measure": REVENUE}, "as": "revenue"}],
        "where": [{"field": STORE_NAME, "op": op, "value": "Philadelphia"}],
    }
    try:
        report = engine.validate_internal(query)
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_QUERY"
        with pytest.raises(SemanticLayerError):
            engine.compile_internal(query)
    finally:
        engine.close()


# --- G4: a dimension declared filterable: false filters no call ----------------------------


@pytest.fixture(scope="module")
def unfilterable(config):
    """Customer and product type are groupable but not filterable; a package metric still
    filters on customer type."""
    dimensions = [
        replace(row, filterable=False) if row.id in {CUSTOMER_TYPE, PRODUCT_TYPE} else row
        for row in config.dimensions
    ]
    template = config.metric_recipes[0]
    returning_revenue = replace(
        template,
        id="metric.sales.returning_revenue",
        expression=parse_semantic_expression(
            {"kind": "aggregate", "measure": REVENUE, "filter": {"all": [RETURNING]}},
            context="config",
        ),
    )
    return replace(
        config, dimensions=dimensions, metric_recipes=[*config.metric_recipes, returning_revenue]
    )


def _filtered(where=None, expression=None):
    return {
        "select": [{"expression": expression or REVENUE, "as": "revenue"}],
        **({"where": where} if where else {}),
    }


CONDITION_ON_TYPE = {
    "kind": "aggregate_if",
    "aggregation": "sum",
    "condition": {
        "kind": "comparison",
        "op": "=",
        "left": {"kind": "column", "table": "jaffle_customer", "column": "customer_type"},
        "right": {"kind": "literal", "value": "returning"},
    },
    "value": {"kind": "column", "table": "jaffle_order", "column": "order_total"},
}
UNFILTERABLE_CALLS = {
    "where": _filtered(where=[RETURNING]),
    "where_missing": _filtered(where=[{"field": CUSTOMER_TYPE, "op": "missing", "value": True}]),
    "entity_any": _filtered(
        where=[
            {
                "entity": "entity.jaffle_item",
                "any": [{"field": PRODUCT_TYPE, "op": "is", "value": "jaffle"}],
            }
        ]
    ),
    "aggregate_filter": _filtered(
        expression={"kind": "aggregate", "measure": REVENUE, "filter": {"all": [RETURNING]}}
    ),
    "renamed_field": _filtered(
        expression={
            "kind": "aggregate",
            "measure": REVENUE,
            "filter": {"all": [{**RETURNING, "field": "Customer type"}]},
        }
    ),
    "scoped_where": _filtered(
        expression={"kind": "scoped_aggregate", "measure": REVENUE, "where": [RETURNING]}
    ),
    "aggregate_if_condition": _filtered(expression=CONDITION_ON_TYPE),
}


@pytest.mark.parametrize("call", sorted(UNFILTERABLE_CALLS))
def test_an_unfilterable_dimension_filters_no_call(unfilterable, call):
    engine = Runtime.from_config(unfilterable, source_path=PACKAGE)
    try:
        error = _refusal(engine, UNFILTERABLE_CALLS[call])
    finally:
        engine.close()
    assert _violations(error) == ["not_filterable"]
    expected = PRODUCT_TYPE if call == "entity_any" else CUSTOMER_TYPE
    assert error["details"]["policy_violations"][0]["disallowed"] == [expected]


def test_an_unfilterable_dimension_still_groups_and_package_metrics_still_filter(unfilterable):
    engine = Runtime.from_config(unfilterable, source_path=PACKAGE)
    try:
        assert engine.validate({"select": [REVENUE], "group_by": [CUSTOMER_TYPE]})["ok"]
        assert engine.validate({"select": ["metric.sales.returning_revenue"]})["ok"]
    finally:
        engine.close()


# --- Inline filters meet allowed_where whatever the field is called ------------------------


def test_a_renamed_inline_field_meets_allowed_where_by_its_id(config):
    engine = _engine(config, {"allowed_where": [STORE_NAME]})
    allowed = {
        "kind": "aggregate",
        "measure": REVENUE,
        "filter": {"all": [{**PHILADELPHIA, "field": "Store name"}]},
    }
    denied = {
        "kind": "aggregate",
        "measure": REVENUE,
        "filter": {"all": [{**RETURNING, "field": "Customer type"}]},
    }
    try:
        assert engine.validate(_filtered(expression=allowed))["ok"]
        error = _refusal(engine, _filtered(expression=denied))
    finally:
        engine.close()
    assert _violations(error) == ["disallowed_where"]
    assert error["details"]["policy_violations"][0]["disallowed"] == [CUSTOMER_TYPE]
