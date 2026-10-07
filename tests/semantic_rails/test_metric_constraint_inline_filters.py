"""A metric constraint governs the filters a caller writes inside an expression."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import date

import duckdb
import pytest

from semantic_rails import compiler
from semantic_rails.compiler_parts import bind
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config

PACKAGE = "configs/semantic_rails/jaffle_shop"
REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
INVENTORY = "measure.jaffle.inventory_on_hand_eop"
INVENTORY_DAY = "temporal_role.jaffle_inventory_day"
INVENTORY_STORE = "dimension.jaffle_store_inventory_snapshot_store_id"
STORE_NAME = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER = "entity.jaffle_order"
CUSTOMER = "entity.jaffle_customer"
AOV = "metric.sales.aov_usd"
RETURNING = {"field": CUSTOMER_TYPE, "op": "=", "value": "returning"}


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config(PACKAGE), semantic_policies=[])


def _constrained(config, constraint, governed, source_path=PACKAGE):
    """``governed=None`` makes the policy package-wide."""
    policy = SemanticPolicyConfig(
        id="policy.test.inline_filters",
        kind="metric_constraint",
        object_ids=[governed] if governed else [],
        config=constraint,
    )
    return Runtime.from_config(replace(config, semantic_policies=[policy]), source_path=source_path)


def _assert_denied_before_output(engine, monkeypatch, query):
    def no_output(*args, **kwargs):
        pytest.fail("a constrained inline filter reached output")

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


def _closing_inventory(store_filter):
    return {
        "select": [
            {
                "expression": {
                    "kind": "semi_additive",
                    "measure": INVENTORY,
                    "filter": {"all": [store_filter]},
                },
                "as": "inventory",
            }
        ],
        "time": {"temporal_role": INVENTORY_DAY, "grain": "month"},
    }


def test_an_inline_filter_outside_allowed_where_is_refused(config, monkeypatch):
    """The closing-day value of a stock filtered inside its own expression."""
    query = _closing_inventory({"field": STORE_NAME, "op": "=", "value": "Brooklyn"})
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(config, {"allowed_where": [INVENTORY_STORE]}, INVENTORY)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [STORE_NAME],
            "allowed": [INVENTORY_STORE],
            "source": "inline_expression",
        }
    ]


FILTERED_REVENUE = {"kind": "aggregate", "measure": REVENUE, "filter": {"all": [RETURNING]}}


def _select(expression, **query):
    return {"select": [{"expression": expression, "as": "value"}], **query}


# Each places a filter on customer type inside an expression that reads the governed
# measure: revenue, or order count for a conversion operand.
INLINE_SHAPES = {
    "aggregate": _select(FILTERED_REVENUE),
    "semi_additive": _select({**FILTERED_REVENUE, "kind": "semi_additive"}),
    "scoped_where": _select({"kind": "scoped_aggregate", "measure": REVENUE, "where": [RETURNING]}),
    "nested": _select(
        {"kind": "ratio", "numerator": FILTERED_REVENUE, "denominator": {"measure": REVENUE}}
    ),
    "window_input": _select(
        {"kind": "cumulative", "input": FILTERED_REVENUE},
        time={"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    ),
    "metric_filter": {
        **_select({"measure": ORDERS}),
        "metric_filters": [{"expression": FILTERED_REVENUE, "op": ">", "value": 0}],
    },
    "predicate_input": _select(
        {
            "kind": "scoped_aggregate",
            "measure": ORDERS,
            "predicates": [{"entity": CUSTOMER, "input": FILTERED_REVENUE, "op": ">", "value": 0}],
        }
    ),
    "conversion_operand": _select(
        {
            "kind": "conversion",
            "base": {"measure": "measure.jaffle.session_starts"},
            "converted": {"kind": "aggregate", "measure": ORDERS, "filter": {"all": [RETURNING]}},
            "entity": CUSTOMER,
            "window": {"unit": "day", "value": 28},
            "matching_mode": "first_converted_after_base",
        }
    ),
}

# Each constraint key, and the violation the inline filter above meets.
CONSTRAINTS = {
    "allowed_where": ({"allowed_where": [STORE_NAME]}, "disallowed_where"),
    "allow_metric_filters": ({"allow_metric_filters": False}, "metric_filters_not_allowed"),
    "allowed_metric_filter_entities": (
        {"allowed_metric_filter_entities": [ORDER]},
        "disallowed_metric_filter_entity",
    ),
    # The inline filter names the required field, but cuts only its own leaf.
    "required_where": ({"required_where": [CUSTOMER_TYPE]}, "missing_required_where"),
}


@pytest.mark.parametrize("key", sorted(CONSTRAINTS))
@pytest.mark.parametrize("shape", sorted(INLINE_SHAPES))
def test_constraint_keys_govern_inline_filters(config, monkeypatch, shape, key):
    query = INLINE_SHAPES[shape]
    assert compiler.compile_query(config, None, query)["sql"]
    constraint, kind = CONSTRAINTS[key]
    engine = _constrained(config, constraint, ORDERS if shape == "conversion_operand" else REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert kind in [row["kind"] for row in violations]
    if key == "allowed_where":
        assert violations == [
            {
                "kind": "disallowed_where",
                "disallowed": [CUSTOMER_TYPE],
                "allowed": [STORE_NAME],
                "source": "inline_expression",
            }
        ]


def test_a_metric_read_by_an_inline_filter_meets_the_metric_allowlist(config, monkeypatch):
    predicate = {"kind": "metric_predicate", "entity": CUSTOMER, "input": {"metric": AOV}}
    expression = {
        "kind": "aggregate",
        "measure": REVENUE,
        "filter": {"all": [{"expression": {**predicate, "op": ">", "value": 0}}]},
    }
    query = _select(expression)
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(config, {"allowed_metric_filter_metrics": []}, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert {"kind": "disallowed_metric_filter_metric", "disallowed": [AOV], "allowed": []} in (
        violations
    )


def test_every_inline_filter_is_a_metric_filter_cut(config, monkeypatch):
    """Even one the compiler records no cut for."""
    monkeypatch.setattr(bind, "binding_cut", nullcontext)
    query = INLINE_SHAPES["aggregate"]
    assert not compiler.bind_query(config, None, query).cuts
    engine = _constrained(config, {"allow_metric_filters": False}, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "metric_filters_not_allowed",
            "metric_filter_refs": {},
            "source": "inline_expression",
        }
    ]


CONDITIONAL = {
    "kind": "aggregate_if",
    "aggregation": "count",
    "condition": {
        "kind": "not_in",
        "expr": {"kind": "column", "table": "jaffle_customer", "column": "customer_id"},
        "values": [0],
    },
}


@pytest.mark.parametrize("governed", [None, REVENUE])
def test_a_conditional_aggregate_condition_is_never_an_allowed_field(config, monkeypatch, governed):
    """Its condition reads columns, not fields: refused when it filters a governed leaf.

    Revenue does not read this customer key; its measure constraint leaves it a sibling."""
    query = {
        "select": [
            {"expression": {"measure": REVENUE}, "as": "revenue"},
            {"expression": CONDITIONAL, "as": "customers"},
        ]
    }
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, governed)
    try:
        if governed:
            assert engine.validate(query)["ok"]
            return
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": ["aggregate_if.condition"],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


@pytest.mark.parametrize("placement", ["select", "metric_filter"])
def test_an_inline_filter_on_a_sibling_counts_when_query_wide(config, monkeypatch, placement):
    """A select sibling keeps its own cut; metric_filters govern the whole query."""
    filtered_orders = {"kind": "aggregate", "measure": ORDERS, "filter": {"all": [RETURNING]}}
    query = _select({"measure": REVENUE})
    if placement == "select":
        query["select"].append({"expression": filtered_orders, "as": "orders"})
    else:
        query["metric_filters"] = [{"expression": filtered_orders, "op": ">", "value": 0}]
    query["group_by"] = [STORE_NAME]
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, REVENUE)
    try:
        if placement == "metric_filter":
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations == [
                {
                    "kind": "disallowed_where",
                    "disallowed": [CUSTOMER_TYPE],
                    "allowed": [STORE_NAME],
                    "source": "inline_expression",
                }
            ]
            return
        result = engine.validate(query)
        assert result["ok"], result["errors"]
    finally:
        engine.close()


FILTERED_ORDERS = {"kind": "aggregate", "measure": ORDERS, "filter": {"all": [RETURNING]}}
RETURNING_CUSTOMER = {
    "kind": "metric_predicate",
    "entity": CUSTOMER,
    "input": FILTERED_ORDERS,
    "scope_mode": "entity_only",
    "op": ">",
    "value": 0,
}
WHOLE_QUERY_FILTERS = {
    "aggregate_predicate": _select(
        {
            "kind": "aggregate",
            "measure": REVENUE,
            "filter": {"all": [{"expression": RETURNING_CUSTOMER}]},
        }
    ),
    "scoped_predicate": _select(
        {"kind": "scoped_aggregate", "measure": REVENUE, "predicates": [RETURNING_CUSTOMER]}
    ),
    "metric_filter_predicate": _select(
        {"measure": REVENUE},
        metric_filters=[{"expression": RETURNING_CUSTOMER, "op": "=", "value": True}],
    ),
    "conversion_base": _select(
        {
            "kind": "conversion",
            "base": {
                "kind": "aggregate",
                "measure": "measure.jaffle.session_starts",
                "filter": {"all": [RETURNING]},
            },
            "converted": {"measure": ORDERS},
            "entity": CUSTOMER,
            "window": {"unit": "day", "value": 28},
            "matching_mode": "first_converted_after_base",
        }
    ),
}


@pytest.mark.parametrize("shape", sorted(WHOLE_QUERY_FILTERS))
def test_nested_filter_counts_for_the_governed_outer_measure(config, monkeypatch, shape):
    query = WHOLE_QUERY_FILTERS[shape]
    assert compiler.compile_query(config, None, query)["sql"]
    governed = ORDERS if shape == "conversion_base" else REVENUE
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [CUSTOMER_TYPE],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


def test_an_inline_filter_on_an_allowed_field_still_answers(tmp_path):
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = replace(load_package_config(str(package)), semantic_policies=[])
    stores = ["Brooklyn", "Philadelphia"]
    query = _closing_inventory({"field": STORE_NAME, "op": "in", "value": stores})
    engine = _constrained(
        config, {"allowed_where": [STORE_NAME]}, INVENTORY, source_path=str(package)
    )
    try:
        rows = engine.query(query)["rows"]
        db_path = engine.db_path
    finally:
        engine.close()
    # Choose each store's last snapshot first, then sum the selected stores.
    # An observed month with no selected store is 0; an unobserved month is absent.
    with duckdb.connect(db_path, read_only=True) as connection:
        reference = connection.execute(
            """
            WITH snapshots AS (
                SELECT i.store_id, DATE_TRUNC('month', i.date_day) AS month, i.date_day,
                    i.inventory_on_hand, s.store_name
                FROM jaffle_store_inventory_snapshot i JOIN jaffle_store s USING (store_id)
            ),
            closing AS (
                SELECT store_id, month, MAX(date_day) AS date_day FROM snapshots GROUP BY 1, 2
            )
            SELECT month,
                CASE WHEN COUNT(*) FILTER (WHERE store_name IN (?, ?)) = 0 THEN 0
                    ELSE SUM(inventory_on_hand) FILTER (WHERE store_name IN (?, ?))
                END
            FROM snapshots JOIN closing USING (store_id, month, date_day)
            GROUP BY 1 ORDER BY 1
            """,
            stores * 2,
        ).fetchall()
    assert len(reference) == 9
    actual = dict(
        (date.fromisoformat(str(row[f"{INVENTORY_DAY}__month"])[:10]), row["inventory"])
        for row in rows
    )
    expected = {date.fromisoformat(str(month)[:10]): float(value) for month, value in reference}
    assert actual == pytest.approx(expected)
