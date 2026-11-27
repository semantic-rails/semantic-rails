"""A metric constraint governs the filters a caller writes inside an expression."""

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import date

import duckdb
import pytest

from semantic_rails import compiler, policies
from semantic_rails.compiler_parts import bind
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    CallExpr,
    ColumnRefExpr,
    LiteralExpr,
    parse_semantic_expression,
)
from semantic_rails.mcp import SemanticLayerMCPAdapter
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
INVENTORY_SNAPSHOT = "entity.jaffle_store_inventory_snapshot"
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
    """``governed=None`` makes the policy package-wide; a list governs each id in it."""
    policy = SemanticPolicyConfig(
        id="policy.test.inline_filters",
        kind="metric_constraint",
        object_ids=governed if isinstance(governed, list) else [governed] if governed else [],
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


UNDECLARED_TOTAL = f"{ORDER}.order_total_cents"
# The shapes that multiply the order total, a column no dimension declares, also name it, as do
# those where the customer's revenue decides what else appears (revenue beside it, the stores).
COMPUTES_TOTAL = {
    "arithmetic": [UNDECLARED_TOTAL, ORDER_CUSTOMER_ID],
    "case_in_arithmetic": [ORDER_CUSTOMER_ID, UNDECLARED_TOTAL],
    "entity_filter_input": [ORDER_CUSTOMER_ID, UNDECLARED_TOTAL],
    "ratio": [ORDER_CUSTOMER_ID, UNDECLARED_TOTAL],
}
# Revenue's constraint also holds the customer's revenue in the ratio, whose own column is free.
ALSO_HOLDS = {"ratio": [[ORDER_CUSTOMER_ID]]}


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
            "disallowed": disallowed,
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
        for disallowed in [
            COMPUTES_TOTAL.get(shape, [ORDER_CUSTOMER_ID]),
            *ALSO_HOLDS.get(shape, []),
        ]
    ]
    if shape in COMPUTES_TOTAL:
        return  # its message also names the total (test_a_column_no_dimension_declares_…)
    assert (
        message
        == condition_message
        == (
            f"{REVENUE} can't be filtered by {ORDER_CUSTOMER_ID} inside an expression; it can be "
            f"filtered only by {LARGE_ORDER} or {STORE_NAME}."
        )
    )


def test_a_case_in_a_value_on_a_column_no_dimension_declares_is_refused(config, monkeypatch):
    """As in the condition: no allowed_where lists a column no dimension declares."""
    subtotal = {"kind": "column", "entity": ORDER, "column": "subtotal_cents"}
    over = {"kind": "comparison", "op": ">", "left": subtotal, "right": ZERO}
    query = _select(_summed(_case(over, TOTAL, ZERO)), group_by=[STORE_NAME])
    engine = _constrained(config, BY_STORE_OR_SIZE, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [f"{ORDER}.subtotal_cents"],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]


def test_a_column_computed_with_through_another_entity_of_its_table_is_its_dimension(
    config, monkeypatch
):
    """A second entity over the orders table declares no dimension over the customer id: the
    column is still the order's customer id dimension."""
    twin = "entity.test_order_twin"
    order = next(row for row in config.entities if row.id == ORDER)
    package = replace(config, entities=[*config.entities, replace(order, id=twin)])
    total, customer = ({**column, "entity": twin} for column in (TOTAL, ORDER_CUSTOMER))
    only_the_customer = json.loads(json.dumps(ONLY_THE_CUSTOMER).replace(ORDER, twin))
    assert only_the_customer["right"]["left"]["args"][0]["args"][0] == customer
    query = _select(_summed(_times(total, only_the_customer)))
    _compiles(package, query)
    engine = _constrained(package, BY_STORE_OR_SIZE, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            # The total it multiplies is a column no dimension declares, on either entity.
            "disallowed": [f"{twin}.order_total_cents", ORDER_CUSTOMER_ID],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]


# A value that picks rows only by an allowed field, or returns a column as it is (what is
# aggregated, by a count only a key or the column its governed measure is as it is), filters by
# nothing else.
VALUE_NEIGHBOURS = {
    "case_on_an_allowed_field": (REVENUE, _summed(_case(IS_LARGE, TOTAL, ZERO))),
    "returned_total": (REVENUE, _summed(TOTAL)),
    "count_of_the_key": (
        ORDERS,
        {
            "kind": "aggregate_if",
            "aggregation": "count",
            "value": {"kind": "column", "entity": ORDER, "column": "order_id"},
        },
    ),
    "sum_under_a_count_constraint": (
        ORDERS,
        _summed({"kind": "column", "entity": ORDER, "column": "subtotal_cents"}),
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
    "returned_by_an_else_branch": (
        "measure.jaffle.ordering_customer_count",
        {
            "kind": "aggregate_if",
            "aggregation": "count_distinct",
            "value": _case(IS_LARGE, {"kind": "literal", "value": None}, ORDER_CUSTOMER),
        },
    ),
}


# Now refused (by design): each value is off the allow-list, a case, a column revenue divides or
# a count doesn't count, or a distinct count of a column that is no whole key.
OFF_THE_LIST_NEIGHBOURS = {
    "case_on_an_allowed_field",
    "returned_total",
    "sum_under_a_count_constraint",
    "returned_key",
    "returned_by_a_case_branch",
    "returned_by_an_else_branch",
}
OFF_THE_LIST_VALUE = "an aggregate_if whose value isn't its measure's own column or a key it counts"
UNKNOWN_CONDITION = "an aggregate_if whose condition reads a field with unknown values"
# A distinct count of a column that is no whole one-column key is refused by the field check too,
# whose violation names the column it counts (the customer id, never the orders' key).
COUNTED_CUSTOMERS = {"returned_key", "returned_by_a_case_branch", "returned_by_an_else_branch"}


def _off_the_list(holds, alias="value"):
    """The allow-list's refusal of select item ``alias``, which holds ``holds``."""
    return {"kind": "disallowed_expression", "read": f"select item '{alias}' holds {holds}"}


def _inline_disallowed(disallowed, allowed):
    """The field check's refusal of ``disallowed`` read inside an expression."""
    return {
        "kind": "disallowed_where",
        "disallowed": disallowed,
        "allowed": allowed,
        "source": "inline_expression",
    }


@pytest.mark.parametrize("shape", sorted(VALUE_NEIGHBOURS))
def test_a_conditional_aggregate_value_that_filters_by_nothing_disallowed_answers(
    config, monkeypatch, shape
):
    governed, expression = VALUE_NEIGHBOURS[shape]
    query = _select(expression, group_by=[STORE_NAME])
    engine = _constrained(config, BY_STORE_OR_SIZE, governed)
    try:
        if shape in OFF_THE_LIST_NEIGHBOURS:
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            customers = _inline_disallowed([ORDER_CUSTOMER_ID], [LARGE_ORDER, STORE_NAME])
            off = customers if shape in COUNTED_CUSTOMERS else _off_the_list(OFF_THE_LIST_VALUE)
            assert violations == [off]
            return
        result = engine.validate(query)
        assert result["ok"], result["errors"]
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


def test_a_case_on_an_allowed_field_in_a_value_returns_its_revenue(tmp_path, monkeypatch):
    """The large orders' total by store: as a case in the value it is off the allow-list and
    refused (by design); as the condition of the total, which a measure sums as it is, it answers
    with the warehouse's numbers."""
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = _with_bare_measures(replace(load_package_config(str(package)), semantic_policies=[]))
    in_a_case = _select(_summed(_case(IS_LARGE, TOTAL, ZERO)), group_by=[STORE_NAME])
    query = _select(_summed(TOTAL, IS_LARGE), group_by=[STORE_NAME])
    engine = _constrained(config, BY_STORE_OR_SIZE, TOTAL_CENTS, source_path=str(package))
    try:
        rows = engine.query(query)["rows"]
        db_path = engine.db_path
        violations = _assert_denied_before_output(engine, monkeypatch, in_a_case)
    finally:
        engine.close()
    assert violations == [_off_the_list(OFF_THE_LIST_VALUE)]
    with duckdb.connect(db_path, read_only=True) as connection:
        reference = connection.execute(
            """
            SELECT s.store_name, SUM(CASE WHEN o.is_large_order THEN o.order_total_cents END)
            FROM jaffle_order o JOIN jaffle_store s USING (store_id)
            GROUP BY 1
            """
        ).fetchall()
    assert len(reference) > 1
    assert {row[STORE_NAME]: row["value"] for row in rows} == dict(reference)


SUBTOTAL = {"kind": "column", "entity": ORDER, "column": "subtotal_cents"}
UNDECLARED_SUBTOTAL = f"{ORDER}.subtotal_cents"
UNDECLARED_TAX = f"{ORDER}.tax_paid_cents"
ONLY = f"it can be filtered only by {LARGE_ORDER} or {STORE_NAME}"
ONE_UNDECLARED = (
    "so no policy can allow it inside an expression: the package author can declare a "
    "dimension over the column and, if this role may use it, list it in allowed_where."
)
SEVERAL_UNDECLARED = (
    "so no policy can allow them inside an expression: the package author can declare a "
    "dimension over each column and, if this role may use them, list them in allowed_where."
)


def _text(value):
    return {
        "kind": "call",
        "name": "CAST",
        "args": [value, {"kind": "literal", "value": "VARCHAR"}],
    }


# 1 for an order whose subtotal is 4800 and less for every other (subtotals have 4 digits).
ONLY_THE_SUBTOTAL = {
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
                        _text(SUBTOTAL),
                        {"kind": "literal", "value": "4800"},
                        {"kind": "literal", "value": ""},
                    ],
                }
            ],
        },
        "right": {"kind": "call", "name": "LENGTH", "args": [_text(SUBTOTAL)]},
    },
}
# Each computes revenue with a column no dimension declares, which no policy can list: the first
# weights each order by its subtotal; the others are ordinary arithmetic, refused as its price.
UNDECLARED_VALUES = {
    "weighted_by_a_column": (
        _summed(_times(TOTAL, ONLY_THE_SUBTOTAL)),
        f"{REVENUE} can't be filtered by {UNDECLARED_TOTAL} and {UNDECLARED_SUBTOTAL} inside an "
        f"expression; {ONLY}. No dimension declares {UNDECLARED_TOTAL} and "
        f"{UNDECLARED_SUBTOTAL}, {SEVERAL_UNDECLARED}",
    ),
    "in_dollars": (
        _summed(
            {
                "kind": "arithmetic",
                "op": "/",
                "left": TOTAL,
                "right": {"kind": "literal", "value": 100.0},
            }
        ),
        f"{REVENUE} can't be filtered by {UNDECLARED_TOTAL} inside an expression; {ONLY}. No "
        f"dimension declares {UNDECLARED_TOTAL}, {ONE_UNDECLARED}",
    ),
    "net_of_tax": (
        _summed(
            {
                "kind": "arithmetic",
                "op": "-",
                "left": TOTAL,
                "right": {"kind": "column", "entity": ORDER, "column": "tax_paid_cents"},
            }
        ),
        f"{REVENUE} can't be filtered by {UNDECLARED_TOTAL} and {UNDECLARED_TAX} inside an "
        f"expression; {ONLY}. No dimension declares {UNDECLARED_TOTAL} and {UNDECLARED_TAX}, "
        f"{SEVERAL_UNDECLARED}",
    ),
}


@pytest.mark.parametrize("shape", sorted(UNDECLARED_VALUES))
def test_a_column_no_dimension_declares_in_a_value_is_refused(config, monkeypatch, shape):
    """A value that computes with a column no dimension declares is refused, as a condition on
    it is, and the refusal says what the package author can do."""
    expression, message = UNDECLARED_VALUES[shape]
    query = _select(expression, group_by=[STORE_NAME])
    _compiles(config, query)
    engine = _constrained(config, BY_STORE_OR_SIZE, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        error = engine.validate(query)["errors"][0]
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": message.split(" can't be filtered by ")[1]
            .split(" inside")[0]
            .split(" and "),
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]
    assert error["message"] == message


def test_a_column_no_dimension_declares_in_a_value_is_a_metric_filter(config, monkeypatch):
    query = _select(UNDECLARED_VALUES["in_dollars"][0], group_by=[STORE_NAME])
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


def test_a_constraint_on_grouping_alone_holds_a_call_to_the_list(config, monkeypatch):
    """Every metric constraint holds a call to the allow-list (now refused, by design): with no
    allowed_where it allows no field inside an expression, and the weighted total is off the list.
    Revenue by id by store and a plain where still answer."""
    query = _select(UNDECLARED_VALUES["weighted_by_a_column"][0], group_by=[STORE_NAME])
    plain = _shown(REVENUE, where=[{"field": CUSTOMER_TYPE, "op": "is", "value": "repeat"}])
    engine = _constrained(config, {"allowed_group_by": [STORE_NAME]}, REVENUE)
    try:
        answered = engine.validate(plain)
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = engine.validate(query)["errors"][0]["message"]
    finally:
        engine.close()
    assert violations == [_off_the_list(OFF_THE_LIST_VALUE)]
    assert message == (
        f"{REVENUE} can't be read this way: select item 'value' holds {OFF_THE_LIST_VALUE}. "
        f"Under its constraint, {policies._ALLOWED_SHAPES}."
    )
    assert answered["ok"], answered["errors"]


def _counted(aggregation, value):
    return {"kind": "aggregate_if", "aggregation": aggregation, "value": value}


# Each counts orders by store under a constraint on the order count, through a column that is
# neither the count's own nor a key: it counts only the orders where that column has a value (an
# average divides by that count).
COUNTED_COLUMNS = {
    "count": (_counted("count", ORDER_CUSTOMER), [ORDER_CUSTOMER_ID]),
    "count_distinct": (_counted("count_distinct", ORDER_CUSTOMER), [ORDER_CUSTOMER_ID]),
    "count_of_a_case_branch": (
        _counted("count", _case(IS_LARGE, ORDER_CUSTOMER)),
        [ORDER_CUSTOMER_ID],
    ),
    "count_of_an_undeclared_column": (_counted("count", SUBTOTAL), [UNDECLARED_SUBTOTAL]),
    "average": (_counted("avg", SUBTOTAL), [UNDECLARED_SUBTOTAL]),
    "ratio_of_a_count": (
        {"kind": "ratio", "numerator": _counted("count", SUBTOTAL), "denominator": ORDERS},
        [UNDECLARED_SUBTOTAL],
    ),
    "count_of_the_key_or_a_column": (
        _counted(
            "count",
            _case(IS_LARGE, {"kind": "column", "entity": ORDER, "column": "order_id"}, SUBTOTAL),
        ),
        [UNDECLARED_SUBTOTAL],
    ),
}


@pytest.mark.parametrize("governed", [ORDERS, None])
@pytest.mark.parametrize("shape", sorted(COUNTED_COLUMNS))
def test_a_count_of_a_column_filters_by_its_presence(config, monkeypatch, shape, governed):
    """Under a package-wide constraint too: the caller's own aggregate is no governed measure."""
    expression, disallowed = COUNTED_COLUMNS[shape]
    query = _select(expression, group_by=[STORE_NAME])
    _compiles(config, query)
    engine = _constrained(config, BY_STORE_OR_SIZE, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = engine.validate(query)["errors"][0]["message"]
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": disallowed,
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]
    subject = ORDERS if governed else "This query"
    line = f"{subject} can't be filtered by {disallowed[0]} inside an expression; {ONLY}."
    if disallowed == [UNDECLARED_SUBTOTAL]:
        line = f"{line[:-1]}. No dimension declares {UNDECLARED_SUBTOTAL}, {ONE_UNDECLARED}"
    assert message == line


TOTAL_CENTS = "measure.test.order_total_cents"
TAX_CENTS = "measure.test.tax_paid_cents"
GUESTS = "measure.test.customers_with_guests"
TAX = {"kind": "column", "entity": ORDER, "column": "tax_paid_cents"}


@pytest.fixture(scope="module")
def bare(config):
    return _with_bare_measures(config)


def _with_bare_measures(config):
    """The orders with two measures that sum a column as it is (the order total and the tax, in
    cents), and a customer count that counts each order with no customer as one guest."""
    measures = {row.id: row for row in config.measures}

    def measure(source, measure_id, expr):
        name = measure_id.removeprefix("measure.")
        return replace(
            measures[source], id=measure_id, name=name, label=name, aliases=[], expr=expr
        )

    guest = CallExpr("COALESCE", [ColumnRefExpr("customer_id"), LiteralExpr("guest")])
    added = [
        measure(REVENUE, TOTAL_CENTS, ColumnRefExpr("order_total_cents")),
        measure("measure.jaffle.tax_paid_usd", TAX_CENTS, ColumnRefExpr("tax_paid_cents")),
        measure("measure.jaffle.ordering_customer_count", GUESTS, guest),
    ]
    return replace(config, measures=[*config.measures, *added])


def _validated(package, constraint, governed, *expressions):
    query = {
        "select": [
            {"expression": expression, "as": f"value_{index}"}
            for index, expression in enumerate(expressions)
        ],
        "group_by": [STORE_NAME],
    }
    engine = _constrained(package, constraint, governed)
    try:
        return engine.validate(query)
    finally:
        engine.close()


def test_a_count_of_a_governed_column_is_held_by_every_measure_that_governs_it(bare, monkeypatch):
    """The order-total measure sums the total as it is, so a count of it counts that measure's own
    rows; the order count counts every order, so for it the same count is the orders with a
    total."""
    query = _select(_counted("count", TOTAL), group_by=[STORE_NAME])
    result = _validated(bare, BY_STORE_OR_SIZE, TOTAL_CENTS, _counted("count", TOTAL))
    assert result["ok"], result["errors"]
    engine = _constrained(bare, BY_STORE_OR_SIZE, [TOTAL_CENTS, ORDERS])
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [UNDECLARED_TOTAL],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]


# Each counts or averages a column its governed measure reads inside an expression: the guest
# count counts orders with no customer id, which a count of the id leaves out, and revenue
# divides the total. Only a key, or the column the measure is as it is, counts freely.
WRAPPED_COLUMNS = {
    "customer_ids_beside_guests": (
        GUESTS,
        _counted("count_distinct", ORDER_CUSTOMER),
        ORDER_CUSTOMER_ID,
    ),
    "orders_with_a_customer_beside_guests": (
        GUESTS,
        _counted("count", ORDER_CUSTOMER),
        ORDER_CUSTOMER_ID,
    ),
    "average_total_under_revenue": (REVENUE, _counted("avg", TOTAL), UNDECLARED_TOTAL),
    "orders_with_a_total_under_revenue": (REVENUE, _counted("count", TOTAL), UNDECLARED_TOTAL),
}


@pytest.mark.parametrize("shape", sorted(WRAPPED_COLUMNS))
def test_a_count_of_a_column_its_governed_measure_wraps_is_a_filter(bare, monkeypatch, shape):
    governed, expression, disallowed = WRAPPED_COLUMNS[shape]
    query = _select(expression, group_by=[STORE_NAME])
    _compiles(bare, query)
    engine = _constrained(bare, BY_STORE_OR_SIZE, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = engine.validate(query)["errors"][0]["message"]
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [disallowed],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]
    line = f"{governed} can't be filtered by {disallowed} inside an expression; {ONLY}"
    assert message.startswith(line)


def test_each_aggregate_counts_freely_only_by_its_own_governed_measure(bare):
    """One constraint on the order total and the tax, both summed as they are: each average
    reads its own measure's column, alone or side by side on one dashboard."""
    averages = [_counted("avg", TOTAL), _counted("avg", TAX)]
    for expressions in [averages[:1], averages[1:], averages]:
        result = _validated(bare, BY_STORE_OR_SIZE, [TOTAL_CENTS, TAX_CENTS], *expressions)
        assert result["ok"], (expressions, result["errors"])


def test_two_averages_each_under_its_own_constraint_answer_side_by_side(bare, monkeypatch):
    """The average order total and the average tax by store, each governed by its own
    constraint allowing store filters: each answers alone and both answer on one dashboard. Each
    constraint holds only its own measure's average, so a filter of the tax average beside the
    order total is refused by the tax constraint alone."""
    averages = [_counted("avg", TOTAL), _counted("avg", TAX)]
    policies = [
        SemanticPolicyConfig(
            id=f"policy.test.{name}",
            kind="metric_constraint",
            object_ids=[governed],
            config={"allowed_where": [STORE_NAME]},
        )
        for name, governed in [("total", TOTAL_CENTS), ("tax", TAX_CENTS)]
    ]
    engine = Runtime.from_config(replace(bare, semantic_policies=policies), source_path=PACKAGE)
    returning_tax = _counted("avg", _case(ONE_CUSTOMER, TAX))
    try:
        for expressions in [averages[:1], averages[1:], averages]:
            query = {
                "select": [
                    {"expression": expression, "as": f"value_{index}"}
                    for index, expression in enumerate(expressions)
                ],
                "group_by": [STORE_NAME],
            }
            result = engine.validate(query)
            assert result["ok"], (expressions, result["errors"])
        query = {
            "select": [
                {"expression": averages[0], "as": "total"},
                {"expression": returning_tax, "as": "tax"},
            ],
            "group_by": [STORE_NAME],
        }
        _assert_denied_before_output(engine, monkeypatch, query)
        denied = [
            (effect["policy_id"], effect["violations"])
            for effect in engine.validate(query)["policy_effects"]
            if effect["action"] == "deny"
        ]
    finally:
        engine.close()
    assert denied == [
        (
            "policy.test.tax",
            [
                {
                    "kind": "disallowed_where",
                    "disallowed": [ORDER_CUSTOMER_ID],
                    "allowed": [STORE_NAME],
                    "source": "inline_expression",
                }
            ],
        )
    ]


LARGE_ORDERS = {"kind": "aggregate_if", "aggregation": "count", "condition": IS_LARGE}
ONE_CUSTOMERS_ORDERS = {"kind": "aggregate_if", "aggregation": "count", "condition": ONE_CUSTOMER}
RETURNING_ORDERS = {"kind": "aggregate", "measure": ORDERS, "filter": {"all": [RETURNING]}}
REVENUE_MEASURE = {"kind": "measure", "measure": REVENUE}
LARGEST_FLAG = {"kind": "aggregate_if", "aggregation": "max", "value": IS_LARGE["left"]}


def _more_than(value, count):
    return {
        "kind": "comparison",
        "op": ">",
        "left": count,
        "right": {"kind": "literal", "value": value},
    }


# Each shows the order total or revenue by store only where a count over a field revenue's
# constraint doesn't allow says so, inside one select item.
GATED_TOTALS = {
    "large_order_case": (_case(_more_than(0, LARGE_ORDERS), _summed(TOTAL)), LARGE_ORDER),
    "customer_cohort_case": (
        _case(_more_than(2, ONE_CUSTOMERS_ORDERS), _summed(TOTAL)),
        ORDER_CUSTOMER_ID,
    ),
    "cohort_case_on_the_measure": (
        _case(_more_than(2, ONE_CUSTOMERS_ORDERS), REVENUE_MEASURE),
        ORDER_CUSTOMER_ID,
    ),
    "cohort_case_on_a_metric": (
        _case(_more_than(2, ONE_CUSTOMERS_ORDERS), AOV),
        ORDER_CUSTOMER_ID,
    ),
    "customer_count_case": (
        _case(_more_than(10, _counted("count_distinct", ORDER_CUSTOMER)), REVENUE_MEASURE),
        ORDER_CUSTOMER_ID,
    ),
    "large_order_flag_case": (
        _case(
            {
                "kind": "comparison",
                "op": "=",
                "left": LARGEST_FLAG,
                "right": {"kind": "literal", "value": True},
            },
            REVENUE_MEASURE,
        ),
        LARGE_ORDER,
    ),
    "the_total_gating_itself": (
        _case(_more_than(100, _summed(TOTAL)), _summed(TOTAL)),
        UNDECLARED_TOTAL,
    ),
    "filtered_count_case": (_case(_more_than(0, RETURNING_ORDERS), REVENUE_MEASURE), CUSTOMER_TYPE),
    "nullif": (
        {
            "kind": "call",
            "name": "NULLIF",
            "args": [REVENUE_MEASURE, _times(ONE_CUSTOMERS_ORDERS, ZERO)],
        },
        ORDER_CUSTOMER_ID,
    ),
    "division": (
        _times(
            REVENUE_MEASURE,
            {
                "kind": "arithmetic",
                "op": "/",
                "left": ONE_CUSTOMERS_ORDERS,
                "right": ONE_CUSTOMERS_ORDERS,
            },
        ),
        ORDER_CUSTOMER_ID,
    ),
}


@pytest.mark.parametrize("shape", sorted(GATED_TOTALS))
def test_a_condition_that_gates_a_governed_value_counts_for_it(config, monkeypatch, shape):
    """With revenue limited to store filters, a count's condition that decides whether the order
    total appears in its select item cuts the total. The same count beside revenue, as its own
    select item, cuts only its own rows and answers. Beside the order total's own sum it is now
    refused (by design): revenue divides the total, so that sum is off the allow-list."""
    gated, disallowed = GATED_TOTALS[shape]
    count = gated["whens"][0]["when"]["left"] if "whens" in gated else ONE_CUSTOMERS_ORDERS
    beside = {"allowed_where": [STORE_NAME]}, REVENUE
    result = _validated(config, *beside, _summed(TOTAL), count)
    assert result["policy_effects"][0]["violations"] == [
        _off_the_list(OFF_THE_LIST_VALUE, "value_0")
    ]
    if count != _summed(TOTAL):
        result = _validated(config, *beside, REVENUE_MEASURE, count)
        assert result["ok"], result["errors"]
    query = _select(gated, group_by=[STORE_NAME])
    _compiles(config, query)
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [disallowed],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


# Each value decides what else appears: a count of customers that gates revenue, under one
# constraint on revenue and the ordering-customer count, whose own column it counts; and having
# on whether a store has a large order.
DECIDING_VALUES = {
    "customer_count_gate": (
        ["measure.jaffle.ordering_customer_count", REVENUE],
        _select(
            _case(_more_than(10, _counted("count_distinct", ORDER_CUSTOMER)), REVENUE_MEASURE),
            group_by=[STORE_NAME],
        ),
        ORDER_CUSTOMER_ID,
    ),
    "large_order_having": (
        REVENUE,
        {
            "select": [
                {"expression": REVENUE, "as": "revenue"},
                {"expression": LARGEST_FLAG, "as": "large"},
            ],
            "group_by": [STORE_NAME],
            "having": [{"field": "large", "op": "is", "value": True}],
        },
        LARGE_ORDER,
    ),
}


@pytest.mark.parametrize("shape", sorted(DECIDING_VALUES))
def test_a_value_that_decides_what_appears_reads_the_columns_it_aggregates(
    config, monkeypatch, shape
):
    """Each column it aggregates is a filter. A gate on a count of the orders' key gates as the
    order count does, but a case is off the allow-list: now refused (by design)."""
    governed, query, disallowed = DECIDING_VALUES[shape]
    key = {"kind": "column", "entity": ORDER, "column": "order_id"}
    orders_gate = _case(_more_than(5, _counted("count", key)), REVENUE_MEASURE)
    result = _validated(config, {"allowed_where": [STORE_NAME]}, REVENUE, orders_gate)
    assert result["policy_effects"][0]["violations"] == [_off_the_list("a case", "value_0")]
    _compiles(config, query)
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [disallowed],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


def test_a_read_in_one_select_item_counts_only_for_what_it_shows(config, monkeypatch):
    """Returning customers' tax per order shares the order count with the average order value
    beside it: the filter counts for the order count in its own item, not for the average."""
    filtered_tax = {
        "kind": "aggregate",
        "measure": "measure.jaffle.tax_paid_usd",
        "filter": {"all": [RETURNING]},
    }
    per_order = {"kind": "ratio", "numerator": filtered_tax, "denominator": ORDERS}
    result = _validated(config, {"allowed_where": [STORE_NAME]}, AOV, per_order, AOV)
    assert result["ok"], result["errors"]
    query = _select(per_order, group_by=[STORE_NAME])
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, ORDERS)
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


def test_a_constraint_matches_governed_sources_once_per_request(bare, monkeypatch):
    """The check holds each count to its own governed measures, matched once for the request:
    a dashboard of many counts matches them as often as one count does."""
    calls = []
    match = policies._synthetic_constraint_sources

    def counted(*args):
        calls.append(args)
        return match(*args)

    monkeypatch.setattr(policies, "_synthetic_constraint_sources", counted)
    # Six counts on the allow-list, each under its own condition on an allowed field.
    flag, literal = (
        IS_LARGE["left"],
        [{"kind": "literal", "value": value} for value in (True, False)],
    )
    conditions = [
        *({**IS_LARGE, "op": op, "right": right} for op in ("=", "!=") for right in literal),
        *({"kind": "in", "expr": flag, "values": [right]} for right in literal),
    ]
    counts = [{"kind": "aggregate_if", "aggregation": "count", "condition": c} for c in conditions]
    matched = []
    for expressions in [counts[:1], counts]:
        calls.clear()
        result = _validated(bare, BY_STORE_OR_SIZE, [ORDERS, TOTAL_CENTS], *expressions)
        assert result["ok"], result["errors"]
        matched.append(len(calls))
    assert matched[0] == matched[1], matched


def test_a_package_wide_constraint_reads_an_average_by_its_own_measures(config):
    """Every object a package-wide constraint meets, the store dimension included, holds the
    average stock to the measures over its column, which read it as it is."""
    stock = {"kind": "column", "entity": INVENTORY_SNAPSHOT, "column": "inventory_on_hand"}
    query = _select(_counted("avg", stock), group_by=[INVENTORY_STORE])
    engine = _constrained(config, {"allowed_where": [INVENTORY_STORE]}, None)
    try:
        result = engine.validate(query)
    finally:
        engine.close()
    assert result["ok"], result["errors"]


@pytest.mark.parametrize("read", ["counted", "computed"])
def test_a_dimension_over_a_relation_of_the_same_name_allows_nothing(config, monkeypatch, read):
    """An archive of the orders in another schema declares no dimension: the live orders'
    customer id dimension, allowed here, doesn't allow the archive's column."""
    archive = "entity.test_archive_order"
    order = next(row for row in config.entities if row.id == ORDER)
    copy = replace(order, id=archive, table="archive.jaffle_order")
    package = replace(config, entities=[*config.entities, copy])
    customer = {"kind": "column", "entity": archive, "column": "customer_id"}
    value = customer if read == "counted" else {"kind": "call", "name": "LOWER", "args": [customer]}
    query = _select(_counted("count_distinct", value))
    _compiles(package, query)
    engine = _constrained(package, {"allowed_where": [STORE_NAME, ORDER_CUSTOMER_ID]}, ORDERS)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [f"{archive}.customer_id"],
            "allowed": [ORDER_CUSTOMER_ID, STORE_NAME],
            "source": "inline_expression",
        }
    ]


@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named_by_a_dimension"])
@pytest.mark.parametrize(
    "expression",
    [
        _counted("count", SUBTOTAL),
        {
            "kind": "aggregate_if",
            "aggregation": "count",
            "condition": {"kind": "comparison", "op": ">", "left": SUBTOTAL, "right": ZERO},
        },
    ],
    ids=["counted", "condition"],
)
def test_a_list_naming_a_column_no_dimension_declares_does_not_allow_it(
    config, monkeypatch, expression, named
):
    """``<entity>.<column>`` names the column in a refusal; listing it allows nothing, even when
    a dimension over another column has that id. The refusal lists only the dimensions that
    allow a filter inside an expression."""
    package = config
    if named:
        store = next(
            row for row in config.dimensions if row.id == "dimension.jaffle_order_store_id"
        )
        alias = replace(store, id=UNDECLARED_SUBTOTAL, name="test_subtotal", aliases=[])
        package = replace(config, dimensions=[*config.dimensions, alias])
    query = _select(expression, group_by=[STORE_NAME])
    engine = _constrained(package, {"allowed_where": [STORE_NAME, UNDECLARED_SUBTOTAL]}, ORDERS)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = engine.validate(query)["errors"][0]["message"]
    finally:
        engine.close()
    allowed = [STORE_NAME, UNDECLARED_SUBTOTAL] if named else [STORE_NAME]
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [UNDECLARED_SUBTOTAL],
            "allowed": allowed,
            "source": "inline_expression",
        }
    ]
    if not named:
        assert message == (
            f"{ORDERS} can't be filtered by {UNDECLARED_SUBTOTAL} inside an expression; it can "
            f"be filtered only by {STORE_NAME}. No dimension declares {UNDECLARED_SUBTOTAL}, "
            f"{ONE_UNDECLARED}"
        )


def _not_filterable(config, read):
    """The package with one dimension that isn't filterable, and a call reading a column of the
    orders: a dimension over the store id named like the undeclared subtotal, or the order's
    customer id with an archive of the orders in another schema."""
    if read in {"condition_on_a_named_column", "computed_named_column"}:
        store = next(
            row for row in config.dimensions if row.id == "dimension.jaffle_order_store_id"
        )
        alias = replace(
            store, id=UNDECLARED_SUBTOTAL, name="test_subtotal", aliases=[], filterable=False
        )
        package = replace(config, dimensions=[*config.dimensions, alias])
        if read == "condition_on_a_named_column":
            condition = {"kind": "comparison", "op": ">", "left": SUBTOTAL, "right": ZERO}
            return package, {"kind": "aggregate_if", "aggregation": "count", "condition": condition}
        return package, _counted("count", {"kind": "call", "name": "ABS", "args": [SUBTOTAL]})
    archive = "entity.test_archive_order"
    order = next(row for row in config.entities if row.id == ORDER)
    customer = next(row for row in config.dimensions if row.id == ORDER_CUSTOMER_ID)
    package = replace(
        config,
        entities=[*config.entities, replace(order, id=archive, table="archive.jaffle_order")],
        dimensions=[
            replace(customer, filterable=False) if row is customer else row
            for row in config.dimensions
        ],
    )
    value = {"kind": "call", "name": "LOWER", "args": [{**ORDER_CUSTOMER, "entity": archive}]}
    return package, _counted("count_distinct", value)


@pytest.mark.parametrize(
    ("read", "refused"),
    [
        ("condition_on_a_named_column", UNDECLARED_SUBTOTAL),
        ("computed_named_column", None),
        ("computed_in_an_archive", ORDER_CUSTOMER_ID),
    ],
)
def test_filterable_false_reads_inline_columns_as_before(config, read, refused):
    """Every caller meets ``filterable: false``, policy or none, and it only refuses, so it reads
    columns as it always has: a condition's undeclared column by its ``<entity>.<column>`` name,
    no undeclared value column, and another schema's table of the same name as the same table."""
    package, expression = _not_filterable(config, read)
    engine = Runtime.from_config(package, source_path=PACKAGE)
    try:
        result = engine.validate(_select(expression))
    finally:
        engine.close()
    if refused is None:
        assert result["ok"], result["errors"]
        return
    error = result["errors"][0]
    assert error["code"] == "POLICY_DENIED"
    assert error["details"]["policy_violations"] == [
        {"kind": "not_filterable", "disallowed": [refused]}
    ]


def test_every_dimension_over_one_column_meets_each_spelling(config, monkeypatch):
    """Two dimensions over the order's customer id spelled in two cases name one DuckDB column:
    either spelling meets both, so allowing one of them allows neither spelling."""
    customer = next(row for row in config.dimensions if row.id == ORDER_CUSTOMER_ID)
    upper = replace(
        customer, id="dimension.test_upper_customer_id", column="CUSTOMER_ID", name="upper_id"
    )
    package = replace(config, dimensions=[*config.dimensions, upper])
    for column in ["customer_id", "CUSTOMER_ID"]:
        ref = {"kind": "column", "entity": ORDER, "column": column}
        query = _select(_counted("count_distinct", ref), group_by=[STORE_NAME])
        engine = _constrained(package, {"allowed_where": [STORE_NAME, upper.id]}, ORDERS)
        try:
            result = engine.validate(query)
        finally:
            engine.close()
        assert result["policy_effects"][0]["violations"] == [
            {
                "kind": "disallowed_where",
                "disallowed": [ORDER_CUSTOMER_ID],
                "allowed": [STORE_NAME, upper.id],
                "source": "inline_expression",
            }
        ], column


def test_a_count_exemption_meets_each_governed_spelling_of_its_column(bare):
    """Two governed measures are the order total as it is, spelled in two cases: one DuckDB
    column, which a count of it counts as each measure's own."""
    total = next(row for row in bare.measures if row.id == TOTAL_CENTS)
    upper = replace(
        total,
        id="measure.test.upper_total",
        name="test.upper_total",
        label="test.upper_total",
        expr=ColumnRefExpr("ORDER_TOTAL_CENTS"),
    )
    package = replace(bare, measures=[*bare.measures, upper])
    result = _validated(
        package, BY_STORE_OR_SIZE, [TOTAL_CENTS, upper.id], _counted("count", TOTAL)
    )
    assert result["ok"], result["errors"]


CUSTOMER_REF = "dimension.test_order_customer_ref"


@pytest.fixture(scope="module")
def quoted(config):
    """The orders with a dimension over a column whose name the SQL quotes (it has a space)."""
    customer = next(row for row in config.dimensions if row.id == ORDER_CUSTOMER_ID)
    reference = replace(
        customer, id=CUSTOMER_REF, column="Customer Ref", name="customer_ref", aliases=[]
    )
    return replace(config, dimensions=[*config.dimensions, reference])


def _spelled(column):
    """A read of the customer reference spelled ``column``: in a case condition, computed with,
    counted, and in the condition of a count of the orders."""
    ref = {"kind": "column", "entity": ORDER, "column": column}
    one = {"kind": "comparison", "op": "=", "left": ref, "right": {"kind": "literal", "value": "x"}}
    lower = {"kind": "call", "name": "LOWER", "args": [ref]}
    return {
        "condition": (REVENUE, _summed(_case(one, TOTAL, ZERO))),
        "computed": (ORDERS, _counted("count_distinct", lower)),
        "counted": (ORDERS, _counted("count", ref)),
        "count_condition": (
            ORDERS,
            {"kind": "aggregate_if", "aggregation": "count", "condition": one},
        ),
    }


READS = ["condition", "computed", "counted", "count_condition"]


@pytest.mark.parametrize("read", READS)
def test_a_quoted_column_in_its_own_spelling_is_its_dimension(quoted, monkeypatch, read):
    """The dimension's own spelling of a quoted name is allowed when the dimension is listed and
    refused naming it when it isn't. Of these reads only the count's condition is on the
    allow-list; with the dimension listed, the other three are now refused (by design)."""
    governed, expression = _spelled("Customer Ref")[read]
    query = _select(expression, group_by=[STORE_NAME])
    _compiles(quoted, query)
    engine = _constrained(quoted, {"allowed_where": [STORE_NAME, CUSTOMER_REF]}, governed)
    try:
        result = engine.validate(query)
    finally:
        engine.close()
    if read == "count_condition":
        assert result["ok"], result["errors"]
    else:
        assert result["policy_effects"][0]["violations"] == [_off_the_list(OFF_THE_LIST_VALUE)]
    engine = _constrained(quoted, BY_STORE_OR_SIZE, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [CUSTOMER_REF],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]


@pytest.mark.parametrize("column", ["customer ref", "CUSTOMER REF"])
@pytest.mark.parametrize("read", READS)
def test_a_quoted_column_in_another_case_is_no_dimension(quoted, monkeypatch, read, column):
    """DuckDB reads "CUSTOMER REF" as the column "Customer Ref", but the SQL binds that spelling
    to no dimension, so it reads none of the dimension's unknown values: listing the dimension
    allows nothing."""
    governed, expression = _spelled(column)[read]
    query = _select(expression, group_by=[STORE_NAME])
    _compiles(quoted, query)
    engine = _constrained(quoted, {"allowed_where": [STORE_NAME, CUSTOMER_REF]}, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [f"{ORDER}.{column}"],
            "allowed": [STORE_NAME, CUSTOMER_REF],
            "source": "inline_expression",
        }
    ]


def test_revenue_with_no_quoted_reference_is_right_or_refused(tmp_path, monkeypatch):
    """The order total by store for the orders with no customer reference, where the dimension over the
    reference reads UNKNOWN as no value. With no constraint its own spelling counts the UNKNOWN
    orders. Under one, a condition inside an expression on a field with unknown values is refused
    in every spelling (now refused, by design): another case, which the SQL reads as the bare
    column, is no dimension's, and the dimension's own declares unknown values."""
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    missing = 'CASE WHEN o."Customer Ref" IS NULL OR o."Customer Ref" = \'UNKNOWN\''
    with duckdb.connect(str(package / "jaffle_shop.duckdb")) as connection:
        connection.execute('ALTER TABLE jaffle_order ADD COLUMN "Customer Ref" VARCHAR')
        connection.execute(
            """
            UPDATE jaffle_order SET "Customer Ref" = CASE hash(order_id) % 3
                WHEN 0 THEN 'UNKNOWN' WHEN 1 THEN NULL ELSE 'C-' || order_id END
            """
        )
        reference, nulls_only = (
            dict(
                connection.execute(
                    f"""
                    SELECT s.store_name, SUM({condition} THEN o.order_total_cents END)
                    FROM jaffle_order o JOIN jaffle_store s USING (store_id) GROUP BY 1
                    """
                ).fetchall()
            )
            for condition in (missing, 'CASE WHEN o."Customer Ref" IS NULL')
        )
    assert reference != nulls_only
    loaded = replace(load_package_config(str(package)), semantic_policies=[])
    customer = next(row for row in loaded.dimensions if row.id == ORDER_CUSTOMER_ID)
    dimension = replace(
        customer,
        id=CUSTOMER_REF,
        column="Customer Ref",
        name="customer_ref",
        aliases=[],
        unknown_values=["UNKNOWN"],
    )
    # The order total in cents, which a measure sums as it is: revenue divides it, so a sum of
    # it under revenue's constraint is off the allow-list.
    package_config = _with_bare_measures(
        replace(loaded, dimensions=[*loaded.dimensions, dimension])
    )

    def revenue_with_no(column):
        ref = {"kind": "column", "entity": ORDER, "column": column}
        yes = {"kind": "literal", "value": True}
        no_value = {"kind": "comparison", "op": "missing", "left": ref, "right": yes}
        return _select(_summed(TOTAL, no_value), group_by=[STORE_NAME])

    free = Runtime.from_config(package_config, source_path=str(package))
    try:
        rows = free.query(revenue_with_no("Customer Ref"))["rows"]
    finally:
        free.close()
    engine = _constrained(
        package_config,
        {"allowed_where": [STORE_NAME, CUSTOMER_REF]},
        TOTAL_CENTS,
        source_path=str(package),
    )
    try:
        own = _assert_denied_before_output(engine, monkeypatch, revenue_with_no("Customer Ref"))
        violations = _assert_denied_before_output(
            engine, monkeypatch, revenue_with_no("CUSTOMER REF")
        )
    finally:
        engine.close()
    assert {row[STORE_NAME]: row["value"] for row in rows} == reference
    assert own == [_off_the_list(UNKNOWN_CONDITION)]
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [f"{ORDER}.CUSTOMER REF"],
            "allowed": [STORE_NAME, CUSTOMER_REF],
            "source": "inline_expression",
        }
    ]


@pytest.mark.parametrize(
    ("warehouse", "authored", "written", "same"),
    [
        *((name, "Customer Ref", "customer ref", True) for name in ("duckdb", "bigquery")),
        *((name, "Customer Ref", "CUSTOMER REF", True) for name in ("databricks", "athena")),
        *((name, "Customer Ref", "customer ref", False) for name in ("postgres", "snowflake")),
        ("clickhouse", "Customer Ref", "customer ref", False),
        # An unquoted name folds everywhere but ClickHouse; a reserved word is quoted.
        ("postgres", "Customer_Ref", "customer_ref", True),
        ("clickhouse", "Customer_Ref", "customer_ref", False),
        ("postgres", "Order", "order", False),
        ("duckdb", "Order", "order", True),
        # Only ASCII letters fold: "Größe" is "GRößE", never "GRÖSSE".
        ("duckdb", "Größe", "GRößE", True),
        ("duckdb", "Größe", "GRÖSSE", False),
        ("duckdb", "customer ref", "customer_ref", False),
    ],
)
def test_two_spellings_name_one_column_as_the_warehouse_reads_names(
    config, warehouse, authored, written, same
):
    """The column key a count exemption and the dimensions over one column compare by; a
    spelling the SQL binds to no dimension still meets none (``_dimensions_over``)."""
    package = replace(config, package=replace(config.package, warehouse=warehouse))
    key = policies._column_key
    assert (key(authored, package) == key(written, package)) is same


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


@pytest.mark.parametrize("placement", ["select", "having"])
def test_a_sibling_conditional_aggregate_counts_only_when_query_wide(
    config, monkeypatch, placement
):
    """Revenue's constraint governs revenue, not a count of one customer's orders beside it: that
    count's condition cuts only its own rows, unless having on it cuts the groups. Beside the
    order total's own sum, which revenue divides, it is now refused (by design): that sum is
    off the allow-list."""
    one_customers_orders = {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": ONE_CUSTOMER,
    }
    query = _select(_summed(TOTAL), group_by=[STORE_NAME])
    query["select"].append({"expression": one_customers_orders, "as": "orders"})
    if placement == "having":
        query["having"] = [{"field": "orders", "op": "gt", "value": 0}]
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, REVENUE)
    try:
        if placement == "select":
            beside_revenue = {**query, "select": [REVENUE, *query["select"][1:]]}
            result = engine.validate(beside_revenue)
            assert result["ok"], result["errors"]
            violations = _assert_denied_before_output(engine, monkeypatch, query)
            assert violations == [_off_the_list(OFF_THE_LIST_VALUE)]
            return
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [ORDER_CUSTOMER_ID],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


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


STORE = "entity.jaffle_store"
STOCK_SOP = "measure.jaffle.inventory_on_hand_sop"
STOCK = {"kind": "column", "entity": INVENTORY_SNAPSHOT, "column": "inventory_on_hand"}


def _stores_where(expression):
    return {"entity": STORE, "input": expression, "op": "gt", "value": 0, "over": "all_time"}


def _filters_deep(expression, depth):
    """``expression`` read ``depth`` entity filters deep, each keeping the stores where the stock
    read under the next one, over all their stock, is above 0. An entity filter reads a caller's
    aggregate only inside an expression of measures like this ratio."""
    for _ in range(depth):
        stock = {"kind": "scoped_aggregate", "measure": STOCK_SOP, "aggregation": "sum"}
        filtered = {**stock, "predicates": [_stores_where(expression)]}
        expression = {"kind": "ratio", "numerator": filtered, "denominator": stock}
    return expression


# Each reads an aggregate inside an entity filter that is itself inside the filter of an order
# count: a scoped aggregate's (two or three filters deep), an aggregate's, and a whole-query one.
NESTED_FILTERS = {
    "scoped": lambda value: _select(
        {"kind": "scoped_aggregate", "measure": ORDERS, "predicates": [_stores_where(value)]}
    ),
    "scoped_three_deep": lambda value: _select(
        {
            "kind": "scoped_aggregate",
            "measure": ORDERS,
            "predicates": [_stores_where(_filters_deep(value, 1))],
        }
    ),
    "aggregate": lambda value: _select(
        {
            "kind": "aggregate",
            "measure": ORDERS,
            "filter": {
                "all": [{"expression": {"kind": "metric_predicate", **_stores_where(value)}}]
            },
        }
    ),
    "metric_filter": lambda value: _select(
        {"measure": ORDERS},
        metric_filters=[
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": STORE,
                    "input": value,
                    "op": ">",
                    "value": 0,
                    **ALL_TIME,
                },
                "op": "=",
                "value": True,
            }
        ],
    ),
}


def _nested(placement, aggregate):
    return NESTED_FILTERS[placement](_filters_deep(aggregate, 1))


def _checked(engine, query):
    internal = "metric_filters" in query
    return (engine.validate_internal if internal else engine.validate)(query)


@pytest.mark.parametrize("placement", sorted(NESTED_FILTERS))
def test_an_aggregate_in_a_nested_entity_filter_is_refused_as_in_the_select(
    config, monkeypatch, placement
):
    """Revenue divides the order total, so an average of the total picks the orders that have
    one, however deep the entity filter that reads it."""
    query = _nested(placement, _counted("avg", TOTAL))
    _compiles(config, query)
    engine = _constrained(config, {"allowed_where": [STORE_NAME]}, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = _checked(engine, query)["errors"][0]["message"]
        selected = engine.validate(_select(_counted("avg", TOTAL)))
    finally:
        engine.close()
    assert violations == selected["policy_effects"][0]["violations"]
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [UNDECLARED_TOTAL],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]
    assert message == selected["errors"][0]["message"]


@pytest.mark.parametrize("placement", sorted(NESTED_FILTERS))
@pytest.mark.parametrize("read", ["order_total", "stock"])
def test_an_aggregate_in_a_nested_entity_filter_filters_by_its_column(bare, placement, read):
    """The order-total measure sums the total as it is, and every measure over the stock reads it
    as it is: an average of either answers in the select. In an entity filter it decides which
    stores are kept, so its column is a filter there, as in a predicate's input at any depth."""
    value, governed = (TOTAL, [TOTAL_CENTS, STOCK_SOP]) if read == "order_total" else (STOCK, None)
    column = (
        UNDECLARED_TOTAL if read == "order_total" else f"{INVENTORY_SNAPSHOT}.inventory_on_hand"
    )
    engine = _constrained(bare, {"allowed_where": [STORE_NAME]}, governed)
    try:
        assert _checked(engine, _select(_counted("avg", value)))["ok"]
        result = _checked(engine, _nested(placement, _counted("avg", value)))
    finally:
        engine.close()
    assert result["errors"][0]["code"] == "POLICY_DENIED"
    assert result["policy_effects"][0]["violations"] == [
        {
            "kind": "disallowed_where",
            "disallowed": [column],
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


ORDER_KEY = {"kind": "column", "entity": ORDER, "column": "order_id"}
# Each returns a column as it is under an aggregation that is not a count, or counts the key.
MORE_VALUE_NEIGHBOURS = {
    "largest_subtotal": (REVENUE, _counted("max", SUBTOTAL)),
    "smallest_subtotal": (REVENUE, _counted("min", SUBTOTAL)),
    "median_subtotal": (REVENUE, _counted("median", SUBTOTAL)),
    "count_of_the_key_in_upper_case": (
        ORDERS,
        _counted("count", {**ORDER_KEY, "column": "ORDER_ID"}),
    ),
    "count_of_the_key_if_large": (ORDERS, {**_counted("count", ORDER_KEY), "condition": IS_LARGE}),
}


@pytest.mark.parametrize("shape", sorted(MORE_VALUE_NEIGHBOURS))
def test_a_returned_column_or_a_counted_key_answers(config, shape):
    """A column no dimension declares returned under max, min or median, and the orders' key
    counted in another case or under a condition on an allowed field."""
    governed, expression = MORE_VALUE_NEIGHBOURS[shape]
    result = _validated(config, BY_STORE_OR_SIZE, governed, expression)
    assert result["ok"], result["errors"]


COUNT_OF_THE_SUBTOTAL = _counted("count", SUBTOTAL)
# A count of the subtotal as a second select item beside a count of the key, and as the input of
# a store filter on the order count.
COUNTS_BESIDE = {
    "beside_a_count_of_the_key": {
        "select": [
            {"expression": _counted("count", ORDER_KEY), "as": "orders"},
            {"expression": COUNT_OF_THE_SUBTOTAL, "as": "with_a_subtotal"},
        ],
        "group_by": [STORE_NAME],
    },
    "entity_filter_input": _select(
        {"measure": ORDERS},
        group_by=[STORE_NAME],
        metric_filters=[
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_store",
                    "input": COUNT_OF_THE_SUBTOTAL,
                    "op": ">",
                    "value": 0,
                    "scope_mode": "entity_only",
                },
                "op": "=",
                "value": True,
            }
        ],
    ),
}


@pytest.mark.parametrize("governed", [ORDERS, None])
@pytest.mark.parametrize("shape", sorted(COUNTS_BESIDE))
def test_a_count_of_a_column_beside_others_filters_by_its_presence(
    config, monkeypatch, shape, governed
):
    query = COUNTS_BESIDE[shape]
    _compiles(config, query)
    engine = _constrained(config, BY_STORE_OR_SIZE, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [UNDECLARED_SUBTOTAL],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]


def test_a_measure_that_wraps_the_column_beside_one_that_does_not_exempts_nothing(
    bare, monkeypatch
):
    """Under one constraint on the order total as it is and revenue, which divides it, each must
    be the column for a count of it to count freely."""
    query = _select(_counted("count", TOTAL), group_by=[STORE_NAME])
    engine = _constrained(bare, BY_STORE_OR_SIZE, [TOTAL_CENTS, REVENUE])
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [UNDECLARED_TOTAL],
            "allowed": [LARGE_ORDER, STORE_NAME],
            "source": "inline_expression",
        }
    ]


@pytest.mark.parametrize(
    "governed", [[AOV], [AOV, TOTAL_CENTS]], ids=["recipe", "recipe_and_total"]
)
def test_having_on_an_average_beside_a_metric_reads_its_column(bare, monkeypatch, governed):
    """The average order value beside the average order total answers under a constraint on the
    metric; having on the average decides which stores appear, so it reads the total, even when
    the constraint also governs the measure that sums it as it is."""
    select = [{"expression": AOV, "as": "aov"}, {"expression": _counted("avg", TOTAL), "as": "a"}]
    query = {"select": select, "group_by": [STORE_NAME]}
    constraint = {"allowed_where": [STORE_NAME]}
    engine = _constrained(bare, constraint, governed)
    try:
        result = engine.validate(query)
        assert result["ok"], result["errors"]
        having = {**query, "having": [{"field": "a", "op": "gt", "value": 0}]}
        violations = _assert_denied_before_output(engine, monkeypatch, having)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [UNDECLARED_TOTAL],
            "allowed": [STORE_NAME],
            "source": "inline_expression",
        }
    ]


def test_two_averages_under_their_own_constraints_return_their_values(tmp_path):
    """The average order total and the average tax by store, each under its own constraint, in
    one call, with some taxes missing: each matches its own average over the orders."""
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "UPDATE jaffle_order SET tax_paid_cents = NULL WHERE hash(order_id) % 3 = 0"
        )
        reference = {
            store: (total, tax)
            for store, total, tax in connection.execute(
                """
                SELECT s.store_name, AVG(o.order_total_cents), AVG(o.tax_paid_cents)
                FROM jaffle_order o JOIN jaffle_store s USING (store_id) GROUP BY 1
                """
            ).fetchall()
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM jaffle_order WHERE tax_paid_cents IS NULL"
        ).fetchone()[0]
    loaded = replace(load_package_config(str(package)), semantic_policies=[])
    constraints = [
        SemanticPolicyConfig(
            id=f"policy.test.{name}",
            kind="metric_constraint",
            object_ids=[governed],
            config={"allowed_where": [STORE_NAME]},
        )
        for name, governed in [("total", TOTAL_CENTS), ("tax", TAX_CENTS)]
    ]
    engine = Runtime.from_config(
        replace(_with_bare_measures(loaded), semantic_policies=constraints),
        source_path=str(package),
    )
    query = {
        "select": [
            {"expression": _counted("avg", TOTAL), "as": "total"},
            {"expression": _counted("avg", TAX), "as": "tax"},
        ],
        "group_by": [STORE_NAME],
    }
    try:
        rows = engine.query(query)["rows"]
    finally:
        engine.close()
    assert len(reference) > 1
    answered = {row[STORE_NAME]: (row["total"], row["tax"]) for row in rows}
    assert answered.keys() == reference.keys()
    for store, values in reference.items():
        assert answered[store] == pytest.approx(values), store


def test_a_role_scoped_constraint_decides_each_request_alone(config):
    """One runtime, a constraint on revenue for analysts only, and the cohort-gated revenue asked
    in turn by an analyst and by finance: each request gets its own role's decision."""
    policy = SemanticPolicyConfig(
        id="policy.test.analysts",
        kind="metric_constraint",
        object_ids=[REVENUE],
        roles=["analyst"],
        config={"allowed_where": [STORE_NAME]},
    )
    engine = Runtime.from_config(replace(config, semantic_policies=[policy]), source_path=PACKAGE)
    query = _select(GATED_TOTALS["customer_cohort_case"][0], group_by=[STORE_NAME])
    try:
        decisions = [
            engine.validate(query, context={"roles": [role]})
            for role in ["analyst", "finance", "analyst", "finance"]
        ]
    finally:
        engine.close()
    assert [result["ok"] for result in decisions] == [False, True, False, True]
    for result in decisions[::2]:
        assert result["errors"][0]["code"] == "POLICY_DENIED"
        assert result["policy_effects"][0]["violations"] == [
            {
                "kind": "disallowed_where",
                "disallowed": [ORDER_CUSTOMER_ID],
                "allowed": [STORE_NAME],
                "source": "inline_expression",
            }
        ]


@pytest.mark.parametrize("read", ["counted", "computed"])
def test_a_dimension_over_a_relation_spelled_in_another_case_allows_nothing(
    config, monkeypatch, read
):
    """Relation names compare as written: an entity over ``JAFFLE_ORDER`` doesn't borrow the
    order's customer id dimension, so a pipeline output and a stored table whose names differ only
    in case never share a dimension. Refused, never answered."""
    upper = "entity.test_upper_order"
    order = next(row for row in config.entities if row.id == ORDER)
    package = replace(
        config, entities=[*config.entities, replace(order, id=upper, table="JAFFLE_ORDER")]
    )
    customer = {"kind": "column", "entity": upper, "column": "customer_id"}
    value = customer if read == "counted" else {"kind": "call", "name": "LOWER", "args": [customer]}
    query = _select(_counted("count_distinct", value))
    _compiles(package, query)
    engine = _constrained(package, {"allowed_where": [STORE_NAME, ORDER_CUSTOMER_ID]}, ORDERS)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [f"{upper}.customer_id"],
            "allowed": [ORDER_CUSTOMER_ID, STORE_NAME],
            "source": "inline_expression",
        }
    ]


def test_a_text_word_on_a_quoted_column_reads_only_its_own_spelling(quoted):
    """``contains`` on the customer reference in its dimension's spelling is a filter on that
    dimension, allowed when it is listed; in another case it compares the column of no dimension
    and is refused."""

    def revenue_containing(column):
        ref = {"kind": "column", "entity": ORDER, "column": column}
        word = {
            "kind": "comparison",
            "op": "contains",
            "left": ref,
            "right": {"kind": "literal", "value": "C-"},
        }
        return _select(_summed(TOTAL, word), group_by=[STORE_NAME])

    # The order total in cents, which a measure sums as it is (a sum of it is off revenue's list).
    package = _with_bare_measures(quoted)
    engine = _constrained(package, {"allowed_where": [STORE_NAME, CUSTOMER_REF]}, TOTAL_CENTS)
    try:
        own = engine.validate(revenue_containing("Customer Ref"))
        other = engine.validate(revenue_containing("CUSTOMER REF"))
    finally:
        engine.close()
    assert own["ok"], own["errors"]
    error = other["errors"][0]
    assert error["code"] == "INVALID_QUERY"
    assert (
        f"'contains' compares a dimension's text; {ORDER}.CUSTOMER REF is the column of no"
        in (error["message"])
    )


# The allow-list. Under a constraint with allowed_where, a select item, having, entity filter or
# predicate input that reads a number it governs is answered only in a shape on the list; the
# refusal of anything else names the shapes.
STORE_ONLY = {"allowed_where": [STORE_NAME]}
IN_BROOKLYN = {"all": [{"field": STORE_NAME, "op": "is", "value": "Brooklyn"}]}
BROOKLYN_REVENUE = {"kind": "aggregate", "measure": REVENUE, "filter": IN_BROOKLYN}
BROOKLYN_ORDERS = {"kind": "aggregate", "measure": ORDERS, "filter": IN_BROOKLYN}
ORDER_MONTHS = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}
HAVING = "having reads other than a number it governs, by id"
ENTITY_FILTER = "an entity filter reads other than a number it governs, by id"


def _shown(*expressions, **call):
    """A call by store showing ``expressions`` as ``value_0``, ``value_1``, …"""
    select = [
        {"expression": expression, "as": f"value_{index}"}
        for index, expression in enumerate(expressions)
    ]
    return {"select": select, "group_by": [STORE_NAME], **call}


def _predicate(expression):
    return {"entity": CUSTOMER, "input": expression, "op": "gt", "value": 0, "over": "all_time"}


# Each refused family, with the part it names: under revenue's constraint (store filters only)
# unless a governed measure is given.
REFUSED_SHAPES = {
    "case": (_shown(_case(_more_than(0, REVENUE_MEASURE), REVENUE_MEASURE)), "a case"),
    "nullif": (_shown({"kind": "nullif", "value": REVENUE, "null_value": ZERO}), "a nullif"),
    "coalesce": (
        _shown({"kind": "call", "name": "COALESCE", "args": [REVENUE, ZERO]}),
        "a coalesce",
    ),
    "rolling": (
        _shown(
            {"kind": "rolling", "input": BROOKLYN_REVENUE, "window": {"unit": "month", "value": 3}},
            time=ORDER_MONTHS,
        ),
        "a rolling",
    ),
    "cumulative": (
        _shown({"kind": "cumulative", "input": BROOKLYN_REVENUE}, time=ORDER_MONTHS),
        "a cumulative",
    ),
    "share": (_shown({"kind": "share", "input": REVENUE, "over": [STORE_NAME]}), "a share"),
    "scoped_aggregate": (
        _shown({"kind": "scoped_aggregate", "measure": REVENUE, "where": [IN_BROOKLYN["all"][0]]}),
        "a scoped_aggregate",
    ),
    # Revenue's read makes the outer aggregate one the list holds.
    "nested_predicate_on_it": (
        _shown(
            {
                "kind": "scoped_aggregate",
                "measure": ORDERS,
                "predicates": [
                    _predicate(
                        {
                            "kind": "scoped_aggregate",
                            "measure": ORDERS,
                            "predicates": [_predicate(BROOKLYN_REVENUE)],
                        }
                    )
                ],
            }
        ),
        "a scoped_aggregate",
    ),
    # Beside revenue, a predicate two inputs deep reads only an id, whatever it reads.
    "nested_predicate_beside_it": (
        _shown(
            {
                "kind": "scoped_aggregate",
                "measure": ORDERS,
                "predicates": [
                    _predicate(
                        {
                            "kind": "scoped_aggregate",
                            "measure": ORDERS,
                            "predicates": [_predicate(BROOKLYN_ORDERS)],
                        }
                    )
                ],
            },
            REVENUE,
        ),
        "a predicate inside another predicate's input",
    ),
    "another_statistic": (
        _shown({"kind": "measure", "measure": REVENUE, "aggregation": "max"}),
        "a measure read with another statistic",
    ),
    "a_predicate_on_it_inside_another_aggregate": (
        _shown(
            {
                "kind": "aggregate",
                "measure": ORDERS,
                "filter": {
                    "all": [
                        {
                            "expression": {
                                "kind": "metric_predicate",
                                "entity": CUSTOMER,
                                "input": REVENUE_MEASURE,
                                "op": ">",
                                "value": 100,
                                "over": "all_time",
                            }
                        }
                    ]
                },
            }
        ),
        "an aggregate filtered by something other than allowed fields",
    ),
    "a_predicate_shorthand_on_it_inside_another_aggregate": (
        _shown(
            {
                "kind": "scoped_aggregate",
                "measure": ORDERS,
                "predicates": [
                    {
                        "entity": CUSTOMER,
                        "measure": REVENUE,
                        "op": "gt",
                        "value": 100,
                        "over": "all_time",
                    }
                ],
            }
        ),
        "a scoped_aggregate",
    ),
}


@pytest.mark.parametrize("family", sorted(REFUSED_SHAPES))
def test_each_shape_off_the_list_is_refused_naming_it(config, monkeypatch, family):
    """Each answers with no constraint; under one it is refused, and the message names the part
    and the shapes the constraint allows."""
    query, holds, *governed = REFUSED_SHAPES[family]
    _compiles(config, query)
    engine = _constrained(config, STORE_ONLY, governed or REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = engine.validate(query)["errors"][0]["message"]
    finally:
        engine.close()
    assert violations == [_off_the_list(holds, "value_0")]
    assert message == (
        f"{(governed or [REVENUE])[0]} can't be read this way: select item 'value_0' holds "
        f"{holds}. Under its constraint, {policies._ALLOWED_SHAPES}."
    )


# Each picks the rows that appear by something other than a governed number by id.
REFUSED_CUTS = {
    "having_on_an_aggregate": (
        _shown(BROOKLYN_REVENUE, having=[{"field": "value_0", "op": "gt", "value": 0}]),
        HAVING,
    ),
    "having_on_another_measure": (
        _shown(REVENUE, ORDERS, having=[{"field": "value_1", "op": "gt", "value": 5}]),
        HAVING,
    ),
    "entity_filter_on_another_measure": (
        _shown(
            REVENUE,
            where=[
                {
                    "entity": CUSTOMER,
                    "having": [{"field": ORDERS, "op": "gt", "value": 5}],
                    "over": "all_time",
                }
            ],
        ),
        ENTITY_FILTER,
    ),
    "order_by_another_statistic_with_a_limit": (
        _shown(
            REVENUE,
            {"kind": "measure", "measure": "measure.jaffle.tax_paid_usd", "aggregation": "max"},
            order_by=[{"field": "value_1"}],
            limit=3,
        ),
        "order_by on 'value_1' picks the rows kept",
    ),
    "order_by_an_aggregate_with_a_limit": (
        _shown(REVENUE, BROOKLYN_ORDERS, order_by=[{"field": "value_1"}], limit=3),
        "order_by on 'value_1' picks the rows kept",
    ),
}


@pytest.mark.parametrize("cut", sorted(REFUSED_CUTS))
def test_a_cut_by_anything_but_a_governed_id_is_refused(config, monkeypatch, cut):
    query, read = REFUSED_CUTS[cut]
    _compiles(config, query)
    engine = _constrained(config, STORE_ONLY, REVENUE)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [{"kind": "disallowed_expression", "read": read}]


# Each is on the list, under revenue's constraint (store filters only).
ON_THE_LIST = {
    "ids": _shown(REVENUE, ORDERS, AOV),
    "aggregates_and_their_ratio": _shown(
        BROOKLYN_REVENUE,
        {"kind": "ratio", "numerator": BROOKLYN_REVENUE, "denominator": BROOKLYN_ORDERS},
    ),
    "arithmetic": _shown(
        {
            "kind": "arithmetic",
            "op": "-",
            "left": {"kind": "ratio", "numerator": REVENUE, "denominator": ORDERS},
            "right": {"kind": "literal", "value": 1},
        }
    ),
    "having_on_revenue": _shown(REVENUE, having=[{"field": "value_0", "op": "gt", "value": 0}]),
    "entity_filter_on_revenue": _shown(
        ORDERS,
        where=[
            {
                "entity": CUSTOMER,
                "having": [{"field": REVENUE, "op": "gt", "value": 5}],
                "over": "all_time",
            }
        ],
    ),
    "order_by_revenue_with_a_limit": _shown(
        REVENUE, BROOKLYN_ORDERS, order_by=[{"field": "value_0"}], limit=3
    ),
    "order_by_an_aggregate_without_a_limit": _shown(
        REVENUE, BROOKLYN_ORDERS, order_by=[{"field": "value_1"}]
    ),
    "a_case_beside_it_on_another_number": _shown(
        REVENUE, _case(_more_than(0, {"kind": "measure", "measure": ORDERS}), ORDERS)
    ),
}


@pytest.mark.parametrize("shape", sorted(ON_THE_LIST))
def test_each_shape_on_the_list_answers(config, shape):
    engine = _constrained(config, STORE_ONLY, REVENUE)
    try:
        result = engine.validate(ON_THE_LIST[shape])
    finally:
        engine.close()
    assert result["ok"], result["errors"]


@pytest.mark.parametrize("total", ["inline", "by_id"])
def test_a_top_n_by_a_forbidden_count_is_refused(bare, monkeypatch, total):
    """The stores ranked by one customer's order count, showing the top five stores' totals: the
    count decides which totals appear. Ordered without a limit the same call answers, and with
    one by the total only when it is the measure by id: an inline total ranks by an inline
    aggregate too."""
    governed, shown = (TOTAL_CENTS, _summed(TOTAL)) if total == "inline" else (REVENUE, REVENUE)
    ranked = _shown(shown, ONE_CUSTOMERS_ORDERS, order_by=[{"field": "value_1"}], limit=5)
    _compiles(bare, ranked)
    engine = _constrained(bare, STORE_ONLY, governed)
    try:
        unlimited = engine.validate({key: ranked[key] for key in ranked if key != "limit"})
        by_total = engine.validate({**ranked, "order_by": [{"field": "value_0"}]})
        violations = _assert_denied_before_output(engine, monkeypatch, ranked)
    finally:
        engine.close()
    assert violations == [
        {"kind": "disallowed_expression", "read": "order_by on 'value_1' picks the rows kept"}
    ]
    assert unlimited["ok"], unlimited["errors"]
    assert by_total["ok"] is (total == "by_id"), by_total["errors"]


def test_a_joined_top_n_by_a_forbidden_count_is_refused(config):
    """The same ranking as a joined call: revenue by store beside one customer's order count by
    store, ordered by the count under a limit. Ranked by revenue, or with no limit, it answers."""
    ranked = {
        "parts": [
            {"select": [REVENUE], "group_by": [STORE_NAME]},
            _select(ONE_CUSTOMERS_ORDERS, group_by=[STORE_NAME]),
        ],
        "join": {"on": [[STORE_NAME, STORE_NAME]], "keep": "all"},
        "order_by": [{"field": "value", "direction": "desc"}],
        "limit": 5,
    }
    engine = _constrained(config, STORE_ONLY, REVENUE)
    try:
        refused = engine.validate(ranked)
        by_revenue = engine.validate({**ranked, "order_by": [{"field": "revenue_usd"}]})
        unlimited = engine.validate({key: ranked[key] for key in ranked if key != "limit"})
    finally:
        engine.close()
    assert refused["errors"][0]["code"] == "POLICY_DENIED"
    assert refused["errors"][0]["message"] == (
        "order_by[0].field: 'value' picks the rows the limit or row cap keeps, and under a metric "
        "constraint a joined call ranks only by a key or by a measure or metric by id in a part "
        "with no where or having"
    )
    assert by_revenue["ok"], by_revenue["errors"]
    assert unlimited["ok"], unlimited["errors"]


@pytest.mark.parametrize("alias", [" orders ", None])
def test_an_engine_form_joined_top_n_reads_the_names_its_answer_gives(config, alias):
    """The engine form names a part's item as its answer does: a padded alias by its trimmed
    name, a missing one by its generated name. Ranked by either under a limit, it is refused."""
    count = {"expression": ONE_CUSTOMERS_ORDERS, **({"as": alias} if alias else {})}
    ranked = {
        "parts": [
            {
                "select": [{"expression": REVENUE_MEASURE, "as": "revenue"}],
                "group_by": [STORE_NAME],
            },
            {"select": [count], "group_by": [STORE_NAME]},
        ],
        "join": {"on": [[STORE_NAME, STORE_NAME]], "keep": "all"},
        "order_by": [{"field": alias.strip() if alias else "expr_1", "direction": "DESC"}],
        "limit": 1,
    }
    engine = _constrained(config, STORE_ONLY, REVENUE)
    try:
        refused = engine.validate_internal(ranked)
        by_revenue = engine.validate_internal({**ranked, "order_by": [{"field": "revenue"}]})
    finally:
        engine.close()
    assert refused["errors"][0]["code"] == "POLICY_DENIED", refused
    assert by_revenue["ok"], by_revenue["errors"]


def test_another_statistic_of_a_measure_of_one_column_answers(bare):
    """The average of a measure that sums one column as it is, written as the measure with avg,
    alone or filtered by an allowed field, reads that column as an aggregate_if of it does."""
    average = {"kind": "measure", "measure": TOTAL_CENTS, "aggregation": "avg"}
    filtered = {
        "kind": "aggregate",
        "measure": TOTAL_CENTS,
        "aggregation": "avg",
        "filter": IN_BROOKLYN,
    }
    result = _validated(bare, STORE_ONLY, TOTAL_CENTS, average, filtered, _counted("avg", TOTAL))
    assert result["ok"], result["errors"]


def test_a_constraint_naming_a_measure_the_call_makes_still_decides(config, monkeypatch):
    """A constraint may list any id, a measure an aggregate_if makes included: the order total's
    sum under revenue is still refused as off the list, and a reference to that made measure by
    its id is no measure by id."""
    total = _summed(TOTAL)
    made, _ = bind._synthetic_conditional_measure(
        parse_semantic_expression(total, context="query"), config
    )
    query = _shown(total)
    engine = _constrained(config, STORE_ONLY, [REVENUE, made])
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [_off_the_list(OFF_THE_LIST_VALUE, "value_0")]


ITEM = "entity.jaffle_item"
ITEM_REVENUE = "measure.jaffle.item_revenue_usd"
PRODUCT = "dimension.jaffle_item_product_name"
ITEM_ORDER_ID = "dimension.jaffle_item_order_id"
DISTINCT_ORDERS = _counted(
    "count_distinct", {"kind": "column", "entity": ITEM, "column": "order_id"}
)
# Product revenue shown only for products enough distinct orders bought, on order lines keyed by
# order and item: the order id is part of the key, never the whole key. The field check refuses
# each too, naming the order id it counts.
PART_OF_A_KEY = {
    "gate": (
        ITEM_REVENUE,
        _select(
            _case(_more_than(2, DISTINCT_ORDERS), {"kind": "measure", "measure": ITEM_REVENUE}),
            group_by=[PRODUCT],
        ),
    ),
    "having": (
        ITEM_REVENUE,
        {
            "select": [
                {"expression": ITEM_REVENUE, "as": "revenue"},
                {"expression": DISTINCT_ORDERS, "as": "orders"},
            ],
            "group_by": [PRODUCT],
            "having": [{"field": "orders", "op": "gt", "value": 2}],
        },
    ),
    "counted": (
        "measure.jaffle.item_count",
        _select(DISTINCT_ORDERS, group_by=[PRODUCT]),
    ),
}


@pytest.mark.parametrize("listed", [True, False], ids=["allowed_where", "no_metric_filters"])
@pytest.mark.parametrize("shape", sorted(PART_OF_A_KEY))
def test_a_distinct_count_of_part_of_a_key_is_refused(config, monkeypatch, shape, listed):
    """Under allowed_where, and under allow_metric_filters: false alone, which gave each column of
    a key the count exemption: a distinct count reads as no filter only a whole one-column key."""
    item = next(row for row in config.entities if row.id == ITEM)
    lines = replace(item, key_columns=["order_id", "item_id"])
    package = replace(config, entities=[lines if row is item else row for row in config.entities])
    governed, query = PART_OF_A_KEY[shape]
    _compiles(package, query)
    constraint = {"allowed_where": [PRODUCT]} if listed else {"allow_metric_filters": False}
    engine = _constrained(package, constraint, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    if listed:
        expected = [_inline_disallowed([ITEM_ORDER_ID], [PRODUCT])]
    elif shape == "having":  # having cuts the rows, a metric filter the head already refuses
        cut = {"kind": "metric_filters_not_allowed", "metric_filter_refs": {"entities": [ITEM]}}
        expected = [cut]
    else:
        refs = {"metric_filter_refs": {}, "source": "inline_expression"}
        expected = [{"kind": "metric_filters_not_allowed", **refs}]
    assert violations == expected


@pytest.mark.parametrize("governed", [REVENUE, TOTAL_CENTS])
def test_a_total_gating_itself_through_nullif_is_refused(bare, monkeypatch, governed):
    """A dashboard blanking stores with no order total: nullif reads the total once, as a case
    that tests and returns it reads it twice; both are off the list."""
    query = _shown({"kind": "nullif", "value": _summed(TOTAL), "null_value": ZERO})
    _compiles(bare, query)
    engine = _constrained(bare, STORE_ONLY, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [_off_the_list("a nullif", "value_0")]


def test_the_average_order_value_dashboard_answers_with_its_numbers(tmp_path):
    """Revenue and the order count, each filtered to repeat customers inside its aggregate (the
    seed's customer types are new and repeat), and their ratio, under one constraint on both that
    allows store and customer type filters. Every number has a value."""
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    loaded = replace(load_package_config(str(package)), semantic_policies=[])
    repeat = {"all": [{"field": CUSTOMER_TYPE, "op": "=", "value": "repeat"}]}
    revenue = {"kind": "aggregate", "measure": REVENUE, "filter": repeat}
    orders = {"kind": "aggregate", "measure": ORDERS, "filter": repeat}
    query = _shown(revenue, {"kind": "ratio", "numerator": revenue, "denominator": orders}, AOV)
    constraint = {"allowed_where": [STORE_NAME, CUSTOMER_TYPE]}
    engine = _constrained(loaded, constraint, [REVENUE, ORDERS], source_path=str(package))
    try:
        rows = engine.query(query)["rows"]
        db_path = engine.db_path
    finally:
        engine.close()
    with duckdb.connect(db_path, read_only=True) as connection:
        reference = connection.execute(
            """
            SELECT s.store_name,
                SUM(o.order_total_cents) FILTER (WHERE c.customer_type = 'repeat') / 100.0,
                SUM(o.order_total_cents) FILTER (WHERE c.customer_type = 'repeat') / 100.0
                    / COUNT(DISTINCT o.order_id) FILTER (WHERE c.customer_type = 'repeat'),
                SUM(o.order_total_cents) / 100.0 / COUNT(DISTINCT o.order_id)
            FROM jaffle_order o JOIN jaffle_store s USING (store_id)
                LEFT JOIN jaffle_customer c USING (customer_id)
            GROUP BY 1
            """
        ).fetchall()
    assert len(reference) > 1
    answered = {row[STORE_NAME]: (row["value_0"], row["value_1"], row["value_2"]) for row in rows}
    assert answered.keys() == {row[0] for row in reference}
    for store, *values in reference:
        assert None not in values and None not in answered[store], store
        assert answered[store] == pytest.approx(tuple(values)), store


NO_METRIC_FILTERS = {"allow_metric_filters": False}
FLAG = {"kind": "column", "entity": ORDER, "column": "is_large_order"}
KEPT_BY_THE_COUNT = "order_by on 'value_1' picks the rows kept"
# Each answered under allow_metric_filters: false alone, which held no call to the list: the top
# five stores by one customer's order count (the total inline, or revenue by id), a total gating
# itself through nullif, and a sum of a 0/1 flag under the order count (its large orders).
UNLISTED_SHAPES = {
    "top_n_inline": (
        TOTAL_CENTS,
        _shown(_summed(TOTAL), ONE_CUSTOMERS_ORDERS, order_by=[{"field": "value_1"}], limit=5),
        {"kind": "disallowed_expression", "read": KEPT_BY_THE_COUNT},
    ),
    "top_n_by_id": (
        REVENUE,
        _shown(REVENUE, ONE_CUSTOMERS_ORDERS, order_by=[{"field": "value_1"}], limit=5),
        {"kind": "disallowed_expression", "read": KEPT_BY_THE_COUNT},
    ),
    "nullif": (
        REVENUE,
        _shown({"kind": "nullif", "value": _summed(TOTAL), "null_value": ZERO}),
        _off_the_list("a nullif", "value_0"),
    ),
    "flag_sum": (ORDERS, _shown(_summed(FLAG)), _off_the_list(OFF_THE_LIST_VALUE, "value_0")),
}


@pytest.mark.parametrize("shape", sorted(UNLISTED_SHAPES))
def test_without_allowed_where_a_constraint_holds_a_call_to_the_list(bare, monkeypatch, shape):
    """Every metric constraint holds a call to the allow-list: with no allowed_where, no field is
    allowed inside an expression. (A distinct count of part of a key is in
    ``test_a_distinct_count_of_part_of_a_key_is_refused``.)"""
    governed, query, violation = UNLISTED_SHAPES[shape]
    _compiles(bare, query)
    engine = _constrained(bare, NO_METRIC_FILTERS, governed)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = engine.validate(query)["errors"][0]["message"]
    finally:
        engine.close()
    assert violations == [violation]
    assert message == (
        f"{governed} can't be read this way: {violation['read']}. Under its constraint, "
        f"{policies._ALLOWED_SHAPES}."
    )


def test_without_allowed_where_ids_a_dimension_and_where_answer_with_their_numbers(tmp_path):
    """Under allow_metric_filters: false alone: revenue and the order count by id, by store, for
    repeat customers in where, all stores, the first by name (a dimension under a limit) and
    the top one by revenue (an id under a limit), each as the warehouse computes it."""
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    loaded = replace(load_package_config(str(package)), semantic_policies=[])
    repeat = [{"field": CUSTOMER_TYPE, "op": "is", "value": "repeat"}]
    calls = {
        "every_store": _shown(REVENUE, ORDERS, where=repeat),
        "first_by_name": _shown(
            REVENUE, ORDERS, where=repeat, order_by=[{"field": STORE_NAME}], limit=1
        ),
        "top_by_revenue": _shown(
            REVENUE,
            ORDERS,
            where=repeat,
            order_by=[{"field": "value_0", "direction": "desc"}],
            limit=1,
        ),
    }
    engine = _constrained(loaded, NO_METRIC_FILTERS, [REVENUE, ORDERS], source_path=str(package))
    try:
        answers = {
            name: [
                (row[STORE_NAME], row["value_0"], row["value_1"])
                for row in engine.query(call)["rows"]
            ]
            for name, call in calls.items()
        }
        db_path = engine.db_path
    finally:
        engine.close()
    with duckdb.connect(db_path, read_only=True) as connection:
        reference = connection.execute(
            """
            SELECT s.store_name, SUM(o.order_total_cents) / 100.0, COUNT(DISTINCT o.order_id)
            FROM jaffle_order o JOIN jaffle_store s USING (store_id)
                JOIN jaffle_customer c USING (customer_id)
            WHERE c.customer_type = 'repeat'
            GROUP BY 1
            """
        ).fetchall()
    assert len(reference) > 1

    def same(rows, expected):
        assert [row[0] for row in rows] == [row[0] for row in expected]
        assert [row[1:] for row in rows] == [pytest.approx(row[1:]) for row in expected]

    same(sorted(answers["every_store"]), sorted(reference))
    same(answers["first_by_name"], sorted(reference)[:1])
    same(answers["top_by_revenue"], sorted(reference, key=lambda row: row[1], reverse=True)[:1])


MAX_TOTAL = {"kind": "measure", "measure": TOTAL_CENTS, "aggregation": "max"}
# The largest single order total, a statistic the order total doesn't declare (it sums one column
# as it is): shown, it answers; where it decides which rows appear, it is refused.
ANOTHER_STATISTIC_CUTS = {
    "order_by_with_a_limit": (
        _shown(TOTAL_CENTS, MAX_TOTAL, order_by=[{"field": "value_1"}], limit=3),
        KEPT_BY_THE_COUNT,
    ),
    "having": (
        _shown(TOTAL_CENTS, MAX_TOTAL, having=[{"field": "value_1", "op": "gt", "value": 100}]),
        HAVING,
    ),
}


@pytest.mark.parametrize("cut", sorted(ANOTHER_STATISTIC_CUTS))
def test_another_statistic_decides_no_rows(bare, monkeypatch, cut):
    """In a position that reads only an id (order_by under a limit, having, a joined call's rank)
    a measure keeps its own statistic; shown, or ordered with no limit, another one answers."""
    query, read = ANOTHER_STATISTIC_CUTS[cut]
    _compiles(bare, query)
    joined = {
        "parts": [
            {"select": [TOTAL_CENTS], "group_by": [STORE_NAME]},
            _select(MAX_TOTAL, group_by=[STORE_NAME]),
        ],
        "join": {"on": [[STORE_NAME, STORE_NAME]], "keep": "all"},
        "order_by": [{"field": "value", "direction": "desc"}],
        "limit": 3,
    }
    engine = _constrained(bare, STORE_ONLY, TOTAL_CENTS)
    try:
        shown = engine.validate(_shown(TOTAL_CENTS, MAX_TOTAL, order_by=[{"field": "value_1"}]))
        ranked = engine.validate(joined)
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [{"kind": "disallowed_expression", "read": read}]
    assert shown["ok"], shown["errors"]
    assert ranked["errors"][0]["code"] == "POLICY_DENIED"
    assert ranked["errors"][0]["message"].startswith("order_by[0].field: 'value' picks the rows")


def _joined(part, **call):
    """Revenue by store beside ``part`` by store, every part's stores kept unless ``call`` says."""
    return {
        "parts": [{"select": [REVENUE], "group_by": [STORE_NAME]}, part],
        "join": {"on": [[STORE_NAME, STORE_NAME]], "keep": "all"},
        **call,
    }


ORDERS_BY_STORE = {"select": [ORDERS], "group_by": [STORE_NAME]}
THE_CUSTOMER = ONE_CUSTOMER["right"]["value"]
RANKED_BY_A_FILTERED_PART = (
    "order_by[0].field: 'order_count' picks the rows the limit or row cap keeps, and under a "
    "metric constraint a joined call ranks only by a key or by a measure or metric by id in a "
    "part with no where or having"
)
KEPT_BY_SOME_PARTS = (
    "join.keep: keeps only the keys some parts have, and under a metric constraint a joined call "
    "keeps every part's keys"
)
# Each picks the stores whose revenue appears by one customer's orders, a part's own filter.
FILTERED_RANKS_AND_KEEPS = {
    "ranked_by_a_part_with_where": (
        _joined(
            {
                **ORDERS_BY_STORE,
                "where": [{"field": ORDER_CUSTOMER_ID, "op": "is", "value": THE_CUSTOMER}],
            },
            order_by=[{"field": "order_count", "direction": "desc"}],
            limit=5,
        ),
        RANKED_BY_A_FILTERED_PART,
    ),
    # Now refused (by design): any where of the ranking part, an allowed field's too. (A part has
    # no having, and an entity filter in a part needs keep.)
    "ranked_by_a_part_with_an_allowed_where": (
        _joined(
            {**ORDERS_BY_STORE, "where": [{"field": STORE_NAME, "op": "is", "value": "Brooklyn"}]},
            order_by=[{"field": "order_count", "direction": "desc"}],
            limit=5,
        ),
        RANKED_BY_A_FILTERED_PART,
    ),
    "kept_by_a_filtered_part": (
        {
            **_joined(
                {
                    **ORDERS_BY_STORE,
                    "where": [{"field": ORDER_CUSTOMER_ID, "op": "is", "value": THE_CUSTOMER}],
                }
            ),
            "join": {"on": [[STORE_NAME, STORE_NAME]], "keep": [1]},
        },
        KEPT_BY_SOME_PARTS,
    ),
    # Now refused (by design): keep is refused for every part under a constraint.
    "kept_by_the_governed_part": (
        {**_joined(ORDERS_BY_STORE), "join": {"on": [[STORE_NAME, STORE_NAME]], "keep": [0]}},
        KEPT_BY_SOME_PARTS,
    ),
}


@pytest.mark.parametrize("call", sorted(FILTERED_RANKS_AND_KEEPS))
def test_a_joined_call_ranked_or_kept_by_a_part_s_own_filter_is_refused(config, call):
    """A joined part's own where picks which revenue appears when the part ranks the rows under
    a limit or decides by keep which keys stay. Ranked by revenue, or by a part with no
    filter of its own, or with every key kept and no limit, the same parts answer."""
    query, message = FILTERED_RANKS_AND_KEEPS[call]
    filtered = query["parts"][1]
    answering = [
        _joined(filtered, order_by=[{"field": "revenue_usd", "direction": "desc"}], limit=5),
        _joined(ORDERS_BY_STORE, order_by=[{"field": "order_count", "direction": "desc"}], limit=5),
        _joined(filtered, order_by=[{"field": "order_count", "direction": "desc"}]),
    ]
    engine = _constrained(config, STORE_ONLY, REVENUE)
    free = Runtime.from_config(config, source_path=PACKAGE)
    try:
        refused = engine.validate(query)
        unconstrained = free.validate(query)
        answers = [engine.validate(row) for row in answering]
    finally:
        engine.close()
        free.close()
    assert refused["errors"][0]["code"] == "POLICY_DENIED"
    assert refused["errors"][0]["message"] == message
    assert unconstrained["ok"], unconstrained["errors"]
    for answer in answers:
        assert answer["ok"], answer["errors"]


def test_a_row_cap_keeps_the_rows_its_order_picks_as_a_limit_does(tmp_path, monkeypatch):
    """The stores ordered by their Brooklyn order count, or by one customer's in a joined part,
    with the rows cut by max_rows instead of a limit (MCP's execute passes a cap on every run): the
    order picks which revenue appears, so each is refused through the runtime and MCP, with and
    without a max_rows argument. With no cap it answers, and ordered by revenue under a cap."""
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    loaded = replace(load_package_config(str(package)), semantic_policies=[])
    ordered = _shown(REVENUE, BROOKLYN_ORDERS, order_by=[{"field": "value_1"}])
    joined = _joined(
        _select(ONE_CUSTOMERS_ORDERS, group_by=[STORE_NAME]), order_by=[{"field": "value"}]
    )
    by_revenue = {**ordered, "order_by": [{"field": "value_0", "direction": "desc"}]}
    engine = _constrained(loaded, STORE_ONLY, REVENUE, source_path=str(package))
    adapter = SemanticLayerMCPAdapter(engine)
    try:
        uncapped = [engine.query(call)["row_count"] for call in (ordered, joined)]
        capped = engine.query(by_revenue, limits={"max_rows": 1})
        through_mcp = adapter.call_tool("execute", {"query": by_revenue, "max_rows": 1})
        monkeypatch.setattr(engine, "_get_adapter", lambda *a, **k: pytest.fail("ran"))
        refusals = []
        for call in (ordered, joined):
            with pytest.raises(SemanticLayerError) as exc:
                engine.query(call, limits={"max_rows": 1})
            refusals.append((exc.value.code, str(exc.value)))
            for arguments in ({"query": call, "max_rows": 1}, {"query": call}):
                error = adapter.call_tool("execute", arguments)["error"]
                refusals.append((error["code"], error["message"]))
    finally:
        engine.close()
    assert all(count > 1 for count in uncapped)
    assert capped["row_count"] == 1 and through_mcp["ok"], through_mcp
    single = (
        f"{REVENUE} can't be read this way: {KEPT_BY_THE_COUNT}. Under its constraint, "
        f"{policies._ALLOWED_SHAPES}."
    )
    ranked = RANKED_BY_A_FILTERED_PART.replace("'order_count'", "'value'")
    assert refusals == [("POLICY_DENIED", single)] * 3 + [("POLICY_DENIED", ranked)] * 3


@pytest.fixture(scope="module")
def sentinel(bare):
    """The orders' customer id dimension reads the source's UNKNOWN as no value."""
    return replace(
        bare,
        dimensions=[
            replace(row, unknown_values=["UNKNOWN"]) if row.id == ORDER_CUSTOMER_ID else row
            for row in bare.dimensions
        ],
    )


UNKNOWN = {"kind": "literal", "value": "UNKNOWN"}
YES = {"kind": "literal", "value": True}
# The orders whose customer id is the source's UNKNOWN, each operator a condition can use.
SENTINEL_CONDITIONS = {
    "=": {"kind": "comparison", "op": "=", "left": ORDER_CUSTOMER, "right": UNKNOWN},
    "!=": {"kind": "comparison", "op": "!=", "left": ORDER_CUSTOMER, "right": UNKNOWN},
    "in": {"kind": "in", "expr": ORDER_CUSTOMER, "values": [UNKNOWN]},
    "missing": {"kind": "comparison", "op": "missing", "left": ORDER_CUSTOMER, "right": YES},
}
BY_STORE_OR_CUSTOMER = {"allowed_where": [STORE_NAME, ORDER_CUSTOMER_ID]}


@pytest.mark.parametrize("op", sorted(SENTINEL_CONDITIONS))
def test_a_condition_inside_an_expression_on_a_field_with_unknown_values_is_refused(
    sentinel, bare, monkeypatch, op
):
    """A condition inside an aggregate_if reads the raw column, so it could isolate the orders
    whose customer id is the UNKNOWN the dimension reads as no value: refused under a constraint
    that lists the dimension, for every operator, as is an aggregate filtered by that dimension.
    The same condition answers on a dimension with no unknown values, and where, which reads the
    dimension with its unknown values, answers."""
    inline = _shown(_summed(TOTAL, SENTINEL_CONDITIONS[op]))
    _compiles(sentinel, inline)
    filtered = _shown(
        {
            "kind": "aggregate",
            "measure": TOTAL_CENTS,
            "filter": {"all": [{"field": ORDER_CUSTOMER_ID, "op": "in", "value": ["UNKNOWN"]}]},
        }
    )
    where = _shown(
        TOTAL_CENTS, where=[{"field": ORDER_CUSTOMER_ID, "op": "in", "value": [THE_CUSTOMER]}]
    )
    plain = _constrained(bare, BY_STORE_OR_CUSTOMER, TOTAL_CENTS)
    engine = _constrained(sentinel, BY_STORE_OR_CUSTOMER, TOTAL_CENTS)
    try:
        without_unknown_values = plain.validate(inline)
        by_where = engine.validate(where)
        message = engine.validate(inline)["errors"][0]["message"]
        violations = _assert_denied_before_output(engine, monkeypatch, inline)
        by_filter = _assert_denied_before_output(engine, monkeypatch, filtered)
    finally:
        engine.close()
        plain.close()
    assert violations == [_off_the_list(UNKNOWN_CONDITION, "value_0")]
    assert by_filter == [
        _off_the_list("an aggregate filtered by a field with unknown values", "value_0")
    ]
    assert message == (
        f"{TOTAL_CENTS} can't be read this way: select item 'value_0' holds {UNKNOWN_CONDITION}. "
        f"Under its constraint, {policies._ALLOWED_SHAPES}."
    )
    assert without_unknown_values["ok"], without_unknown_values["errors"]
    assert by_where["ok"], by_where["errors"]


# The orders of one customer, who ordered at one store only, beside every store's order count:
# two output names that differ only in case, which DuckDB reads as one in the statement's own
# common table expressions, so a rank, a filter or a shown column by one could read the other.
PICKS = {
    "Pick": ONE_CUSTOMERS_ORDERS,
    "pick": {"kind": "measure", "measure": ORDERS},
}
TWINS = "the output names 'Pick' and 'pick' differ only in case"


@pytest.mark.parametrize("totals", [False, True], ids=["plain", "totals"])
def test_output_names_that_differ_only_in_case_are_refused(bare, monkeypatch, totals):
    """Ordered by 'pick' (every store's orders) under a limit, the store kept could be picked by
    'Pick' (one customer's orders, a filter the constraint doesn't allow). Under a constraint the
    call is refused whatever its order; with no constraint, and with names that differ in more
    than case, it answers."""
    select = [{"expression": REVENUE, "as": "revenue"}]
    select += [{"expression": expression, "as": alias} for alias, expression in PICKS.items()]
    query = {
        "select": select,
        "group_by": [STORE_NAME],
        "order_by": [{"field": "pick", "direction": "desc"}],
        "limit": 1,
        **({"totals": [STORE_NAME]} if totals else {}),
    }
    _compiles(bare, query)
    apart = {**query, "select": [*select[:2], {**select[2], "as": "every_order"}]}
    apart["order_by"] = [{"field": "every_order", "direction": "desc"}]
    engine = _constrained(bare, STORE_ONLY, REVENUE)
    try:
        answered = engine.validate(apart)
        violations = _assert_denied_before_output(engine, monkeypatch, query)
        message = engine.validate(query)["errors"][0]["message"]
    finally:
        engine.close()
    assert answered["ok"], answered["errors"]
    assert violations == [{"kind": "disallowed_expression", "read": TWINS}]
    assert message == (
        f"{REVENUE} can't be read this way: {TWINS}. Under its constraint, "
        f"{policies._ALLOWED_SHAPES}."
    )


def test_a_joined_call_whose_output_names_differ_only_in_case_is_refused(config):
    """One customer's order count as 'Pick' in a part with its own where, every store's as 'pick'
    beside revenue in another, ranked by 'pick' under a limit: refused under a constraint on
    revenue, and answered with no constraint."""
    one = [{"field": ORDER_CUSTOMER_ID, "op": "is", "value": THE_CUSTOMER}]
    query = {
        "parts": [
            {
                "select": [{"expression": {"kind": "measure", "measure": ORDERS}, "as": "Pick"}],
                "group_by": [STORE_NAME],
                "where": one,
            },
            {
                "select": [
                    {"expression": REVENUE_MEASURE, "as": "revenue"},
                    {"expression": {"kind": "measure", "measure": ORDERS}, "as": "pick"},
                ],
                "group_by": [STORE_NAME],
            },
        ],
        "join": {"on": [[STORE_NAME, STORE_NAME]], "keep": "all"},
        "order_by": [{"field": "pick", "direction": "desc"}],
        "limit": 1,
    }
    engine = _constrained(config, {"allow_metric_filters": False}, REVENUE)
    free = Runtime.from_config(config, source_path=PACKAGE)
    try:
        refused = engine.validate(query)
        unconstrained = free.validate(query)
    finally:
        engine.close()
        free.close()
    assert refused["errors"][0]["code"] == "POLICY_DENIED"
    assert refused["errors"][0]["message"] == (
        f"parts: {TWINS}, and under a metric constraint a joined call names each column once in "
        "any case"
    )
    assert unconstrained["ok"], unconstrained["errors"]
