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
from semantic_rails.query_call import read_call
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
CUSTOMER_ID = "dimension.jaffle_customer_id"
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


def _compiles(config, query):
    """Without the policy the query compiles (an engine-form query is passed as it is)."""
    payload = query if "metric_filters" in query else read_call(query, config=config)[0]
    assert compiler.compile_query(config, None, payload)["sql"]


def _assert_denied_before_output(engine, monkeypatch, query):
    def no_output(*args, **kwargs):
        pytest.fail("a constrained inline filter reached output")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    # An inline expression as an entity filter's input has no call spelling.
    internal = "metric_filters" in query
    validate = engine.validate_internal if internal else engine.validate
    result = validate(query)
    assert result["errors"][0]["code"] == "POLICY_DENIED"
    operations = (
        (engine.compile_internal, engine.query_internal)
        if internal
        else (engine.compile, engine.query)
    )
    for operation in operations:
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
    _compiles(config, query)
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
    "nested": _select({"kind": "ratio", "numerator": FILTERED_REVENUE, "denominator": REVENUE}),
    "window_input": _select(
        {"kind": "cumulative", "input": FILTERED_REVENUE},
        time={"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    ),
    "having": {
        "select": [ORDERS, {"expression": FILTERED_REVENUE, "as": "value"}],
        "having": [{"field": "value", "op": "gt", "value": 0}],
    },
    "predicate_input": _select(
        {
            "kind": "scoped_aggregate",
            "measure": ORDERS,
            "predicates": [
                {
                    "entity": CUSTOMER,
                    "input": FILTERED_REVENUE,
                    "op": "gt",
                    "value": 0,
                    "over": "all_time",
                }
            ],
        }
    ),
    "conversion_operand": _select(
        {
            "kind": "conversion",
            "base": "measure.jaffle.session_starts",
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
    _compiles(config, query)
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
    predicate = {"kind": "metric_predicate", "entity": CUSTOMER, "input": AOV, "over": "all_time"}
    expression = {
        "kind": "aggregate",
        "measure": REVENUE,
        "filter": {"all": [{"expression": {**predicate, "op": ">", "value": 0}}]},
    }
    query = _select(expression)
    _compiles(config, query)
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
        "values": [{"kind": "literal", "value": 0}],
    },
}


@pytest.mark.parametrize("governed", [None, REVENUE])
def test_a_conditional_aggregate_condition_reads_the_fields_over_its_columns(
    config, monkeypatch, governed
):
    """Each column its condition reads counts as the dimensions over it: refused when one is
    not allowed and it filters a governed leaf.

    Revenue does not read this customer key; its measure constraint leaves it a sibling."""
    query = {
        "select": [
            {"expression": REVENUE, "as": "revenue"},
            {"expression": CONDITIONAL, "as": "customers"},
        ]
    }
    _compiles(config, query)
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
            "disallowed": [CUSTOMER_ID],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


def test_a_conditional_aggregate_condition_on_allowed_fields_passes(config):
    query = {"select": [{"expression": CONDITIONAL, "as": "customers"}]}
    engine = _constrained(config, {"allowed_where": [STORE_NAME, CUSTOMER_ID]}, None)
    try:
        assert engine.validate(query)["ok"]
    finally:
        engine.close()


def test_a_condition_on_a_column_no_dimension_declares_is_refused(config, monkeypatch):
    """A column outside every allowed field cannot be filtered by naming it raw."""
    condition = {
        "kind": "comparison",
        "op": ">",
        "left": {"kind": "column", "table": "jaffle_order", "column": "order_total"},
        "right": {"kind": "literal", "value": 0},
    }
    query = {
        "select": [
            {
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    "condition": condition,
                },
                "as": "orders",
            }
        ]
    }
    _compiles(config, query)
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, None)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": ["entity.jaffle_order.order_total"],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


# Revenue is order totals; a policy lets it be filtered by store and by large orders only.
TOTAL = {"kind": "column", "entity": ORDER, "column": "order_total_cents"}
ORDER_CUSTOMER = {"kind": "column", "entity": ORDER, "column": "customer_id"}
ORDER_CUSTOMER_ID = "dimension.jaffle_order_customer_id"
LARGE_ORDER = "dimension.jaffle_order_is_large_order"
BY_STORE_OR_SIZE = {"allowed_where": [STORE_NAME, LARGE_ORDER]}
ONE_CUSTOMER = {
    "kind": "comparison",
    "op": "=",
    "left": ORDER_CUSTOMER,
    "right": {"kind": "literal", "value": "96c8937f-d99c-4515-acec-db24d07768f9"},
}
IS_LARGE = {
    "kind": "comparison",
    "op": "=",
    "left": {"kind": "column", "entity": ORDER, "column": "is_large_order"},
    "right": {"kind": "literal", "value": True},
}
ZERO = {"kind": "literal", "value": 0}


def _summed(value, condition=None):
    expression = {"kind": "aggregate_if", "aggregation": "sum", "value": value}
    return {**expression, "condition": condition} if condition else expression


def _case(when, then, otherwise=None):
    case = {"kind": "case", "whens": [{"when": when, "then": then}]}
    return {**case, "else": otherwise} if otherwise else case


def _times(left, right):
    return {"kind": "arithmetic", "op": "*", "left": left, "right": right}


# 1 for the one customer's orders and 0 for every other's (ids are 36 characters): no CASE.
ONLY_THE_CUSTOMER = {
    "kind": "arithmetic",
    "op": "-",
    "left": {"kind": "literal", "value": 1},
    "right": {
        "kind": "arithmetic",
        "op": "/",
        "left": {
            "kind": "call",
            "name": "LENGTH",
            "args": [
                {
                    "kind": "call",
                    "name": "REPLACE",
                    "args": [
                        ORDER_CUSTOMER,
                        ONE_CUSTOMER["right"],
                        {"kind": "literal", "value": ""},
                    ],
                }
            ],
        },
        "right": {"kind": "literal", "value": 36},
    },
}
CUSTOMER_REVENUE = _summed(_case(ONE_CUSTOMER, TOTAL, ZERO))
# Each reads one customer's revenue per store through an aggregate_if value, not its condition.
VALUE_FILTERS = {
    "case": _select(CUSTOMER_REVENUE, group_by=[STORE_NAME]),
    "case_without_else": _select(_summed(_case(ONE_CUSTOMER, TOTAL)), group_by=[STORE_NAME]),
    "case_in_arithmetic": _select(
        _summed(_times(TOTAL, _case(ONE_CUSTOMER, {"kind": "literal", "value": 1}, ZERO))),
        group_by=[STORE_NAME],
    ),
    "case_in_a_case_branch": _select(
        _summed(_case(IS_LARGE, _case(ONE_CUSTOMER, TOTAL))), group_by=[STORE_NAME]
    ),
    "arithmetic": _select(_summed(_times(TOTAL, ONLY_THE_CUSTOMER)), group_by=[STORE_NAME]),
    "ratio": _select(
        {"kind": "ratio", "numerator": CUSTOMER_REVENUE, "denominator": REVENUE},
        group_by=[STORE_NAME],
    ),
    "entity_filter_input": _select(
        {"measure": ORDERS},
        metric_filters=[
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_store",
                    "input": CUSTOMER_REVENUE,
                    "op": ">",
                    "value": 100000,
                    "scope_mode": "entity_only",
                },
                "op": "=",
                "value": True,
            }
        ],
    ),
}


@pytest.mark.parametrize("shape", sorted(VALUE_FILTERS))
def test_a_filter_in_a_conditional_aggregate_value_is_refused_like_its_condition(
    config, monkeypatch, shape
):
    """A value that is the order total for one customer's orders and 0 (or nothing) for the
    rest filters revenue by customer as its condition would: the same refusal."""
    query = VALUE_FILTERS[shape]
    _compiles(config, query)
    by_condition = _select(_summed(TOTAL, ONE_CUSTOMER), group_by=[STORE_NAME])
    engine = _constrained(config, BY_STORE_OR_SIZE, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        validate = engine.validate_internal if "metric_filters" in query else engine.validate
        message = validate(query)["errors"][0]["message"]
        condition_message = engine.validate(by_condition)["errors"][0]["message"]
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [ORDER_CUSTOMER_ID],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]
    assert (
        message
        == condition_message
        == (
            f"{REVENUE} can't be filtered by {ORDER_CUSTOMER_ID} inside an expression; it can be "
            f"filtered only by {LARGE_ORDER} or {STORE_NAME}."
        )
    )


# A value that picks rows only by an allowed field, computes only with columns no dimension
# declares, or returns a column as it is (what is aggregated) filters by nothing else.
VALUE_NEIGHBOURS = {
    "case_on_an_allowed_field": (REVENUE, _summed(_case(IS_LARGE, TOTAL, ZERO))),
    "arithmetic_on_value_columns": (
        REVENUE,
        _summed(
            {
                "kind": "arithmetic",
                "op": "-",
                "left": TOTAL,
                "right": {"kind": "column", "entity": ORDER, "column": "tax_paid_cents"},
            }
        ),
    ),
    "returned_key": (
        "measure.jaffle.ordering_customer_count",
        {"kind": "aggregate_if", "aggregation": "count_distinct", "value": ORDER_CUSTOMER},
    ),
    "returned_by_a_case_branch": (
        "measure.jaffle.ordering_customer_count",
        {
            "kind": "aggregate_if",
            "aggregation": "count_distinct",
            "value": _case(IS_LARGE, ORDER_CUSTOMER),
        },
    ),
}


@pytest.mark.parametrize("shape", sorted(VALUE_NEIGHBOURS))
def test_a_conditional_aggregate_value_that_filters_by_nothing_disallowed_answers(config, shape):
    governed, expression = VALUE_NEIGHBOURS[shape]
    query = _select(expression, group_by=[STORE_NAME])
    engine = _constrained(config, BY_STORE_OR_SIZE, governed)
    try:
        result = engine.validate(query)
        assert result["ok"], result["errors"]
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


def test_a_case_on_an_allowed_field_in_a_value_returns_its_revenue(tmp_path):
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = replace(load_package_config(str(package)), semantic_policies=[])
    query = _select(_summed(_case(IS_LARGE, TOTAL, ZERO)), group_by=[STORE_NAME])
    engine = _constrained(config, BY_STORE_OR_SIZE, REVENUE, source_path=str(package))
    try:
        rows = engine.query(query)["rows"]
        db_path = engine.db_path
    finally:
        engine.close()
    with duckdb.connect(db_path, read_only=True) as connection:
        reference = connection.execute(
            """
            SELECT s.store_name, SUM(CASE WHEN o.is_large_order THEN o.order_total_cents ELSE 0 END)
            FROM jaffle_order o JOIN jaffle_store s USING (store_id)
            GROUP BY 1
            """
        ).fetchall()
    assert len(reference) > 1
    assert {row[STORE_NAME]: row["value"] for row in rows} == dict(reference)


@pytest.mark.parametrize("placement", ["select", "having"])
def test_an_inline_filter_on_a_sibling_counts_when_query_wide(config, monkeypatch, placement):
    """A select sibling keeps its own cut; having on it governs the whole query."""
    filtered_orders = {"kind": "aggregate", "measure": ORDERS, "filter": {"all": [RETURNING]}}
    query = _select(REVENUE)
    query["select"].append({"expression": filtered_orders, "as": "orders"})
    if placement == "having":
        query["having"] = [{"field": "orders", "op": "gt", "value": 0}]
    query["group_by"] = [STORE_NAME]
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, REVENUE)
    try:
        if placement == "having":
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
    "op": ">",
    "value": 0,
}
# The engine's own spelling of the call's ``over: all_time``.
ALL_TIME = {"scope_mode": "entity_only"}
WHOLE_QUERY_FILTERS = {
    "aggregate_predicate": _select(
        {
            "kind": "aggregate",
            "measure": REVENUE,
            "filter": {"all": [{"expression": {**RETURNING_CUSTOMER, "over": "all_time"}}]},
        }
    ),
    "scoped_predicate": _select(
        {
            "kind": "scoped_aggregate",
            "measure": REVENUE,
            "predicates": [{**RETURNING_CUSTOMER, "over": "all_time"}],
        }
    ),
    "metric_filter_predicate": _select(
        {"measure": REVENUE},
        metric_filters=[
            {"expression": {**RETURNING_CUSTOMER, **ALL_TIME}, "op": "=", "value": True}
        ],
    ),
    "conversion_base": _select(
        {
            "kind": "conversion",
            "base": {
                "kind": "aggregate",
                "measure": "measure.jaffle.session_starts",
                "filter": {"all": [RETURNING]},
            },
            "converted": ORDERS,
            "entity": CUSTOMER,
            "window": {"unit": "day", "value": 28},
            "matching_mode": "first_converted_after_base",
        }
    ),
}


@pytest.mark.parametrize("shape", sorted(WHOLE_QUERY_FILTERS))
def test_nested_filter_counts_for_the_governed_outer_measure(config, monkeypatch, shape):
    query = WHOLE_QUERY_FILTERS[shape]
    _compiles(config, query)
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
    actual = dict((date.fromisoformat(row["period"]), row["inventory"]) for row in rows)
    expected = {date.fromisoformat(str(month)[:10]): float(value) for month, value in reference}
    assert actual == pytest.approx(expected)
