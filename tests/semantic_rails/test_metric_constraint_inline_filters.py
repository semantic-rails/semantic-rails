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
from semantic_rails.expressions import CallExpr, ColumnRefExpr, LiteralExpr
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


def test_a_constraint_on_grouping_alone_reads_undeclared_columns_as_before(config):
    """Without allowed_where a caller may filter by any field, so nothing lists what to refuse."""
    query = _select(UNDECLARED_VALUES["weighted_by_a_column"][0], group_by=[STORE_NAME])
    engine = _constrained(config, {"allowed_group_by": [STORE_NAME]}, REVENUE)
    try:
        result = engine.validate(query)
    finally:
        engine.close()
    assert result["ok"], result["errors"]


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
    total appears in its select item cuts the total. The same count beside the total, as its own
    select item, cuts only its own rows and answers."""
    gated, disallowed = GATED_TOTALS[shape]
    count = gated["whens"][0]["when"]["left"] if "whens" in gated else ONE_CUSTOMERS_ORDERS
    result = _validated(config, {"allowed_where": [STORE_NAME]}, REVENUE, _summed(TOTAL), count)
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
    """Each column it aggregates is a filter. A count of the orders' key gates as the order
    count does and answers."""
    governed, query, disallowed = DECIDING_VALUES[shape]
    key = {"kind": "column", "entity": ORDER, "column": "order_id"}
    orders_gate = _case(_more_than(5, _counted("count", key)), REVENUE_MEASURE)
    result = _validated(config, {"allowed_where": [STORE_NAME]}, REVENUE, orders_gate)
    assert result["ok"], result["errors"]
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
    counts = [
        _counted("count", _case(IS_LARGE, {"kind": "literal", "value": index}))
        for index in range(6)
    ]
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
    """A read of the customer reference spelled ``column``: in a condition, computed with, and
    counted."""
    ref = {"kind": "column", "entity": ORDER, "column": column}
    one = {"kind": "comparison", "op": "=", "left": ref, "right": {"kind": "literal", "value": "x"}}
    lower = {"kind": "call", "name": "LOWER", "args": [ref]}
    return {
        "condition": (REVENUE, _summed(_case(one, TOTAL, ZERO))),
        "computed": (ORDERS, _counted("count_distinct", lower)),
        "counted": (ORDERS, _counted("count", ref)),
    }


@pytest.mark.parametrize("read", ["condition", "computed", "counted"])
def test_a_quoted_column_in_its_own_spelling_is_its_dimension(quoted, monkeypatch, read):
    """The dimension's own spelling of a quoted name is allowed when the dimension is listed and
    refused naming it when it isn't."""
    governed, expression = _spelled("Customer Ref")[read]
    query = _select(expression, group_by=[STORE_NAME])
    _compiles(quoted, query)
    engine = _constrained(quoted, {"allowed_where": [STORE_NAME, CUSTOMER_REF]}, governed)
    try:
        result = engine.validate(query)
    finally:
        engine.close()
    assert result["ok"], result["errors"]
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
@pytest.mark.parametrize("read", ["condition", "computed", "counted"])
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
    """Revenue by store for the orders with no customer reference, where the dimension over the
    reference reads UNKNOWN as no value. Its own spelling counts the UNKNOWN orders; another case,
    which the SQL reads as the bare column, is refused rather than leaving them out."""
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
    package_config = replace(loaded, dimensions=[*loaded.dimensions, dimension])

    def revenue_with_no(column):
        ref = {"kind": "column", "entity": ORDER, "column": column}
        yes = {"kind": "literal", "value": True}
        no_value = {"kind": "comparison", "op": "missing", "left": ref, "right": yes}
        return _select(_summed(TOTAL, no_value), group_by=[STORE_NAME])

    engine = _constrained(
        package_config,
        {"allowed_where": [STORE_NAME, CUSTOMER_REF]},
        REVENUE,
        source_path=str(package),
    )
    try:
        rows = engine.query(revenue_with_no("Customer Ref"))["rows"]
        violations = _assert_denied_before_output(
            engine, monkeypatch, revenue_with_no("CUSTOMER REF")
        )
    finally:
        engine.close()
    assert {row[STORE_NAME]: row["value"] for row in rows} == reference
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
    """Revenue's constraint governs the order total's sum, not a count of one customer's orders
    beside it: that count's condition cuts only its own rows, unless having on it cuts the
    groups."""
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
            result = engine.validate(query)
            assert result["ok"], result["errors"]
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

    engine = _constrained(quoted, {"allowed_where": [STORE_NAME, CUSTOMER_REF]}, REVENUE)
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
