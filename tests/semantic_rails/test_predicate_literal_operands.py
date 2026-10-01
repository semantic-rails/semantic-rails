"""A literal operand in a metric_predicate input, like ``rate * 100 < 30`` or ``total - 5 > 0``.

Invariant: a literal operand never changes the grain of a predicate input. The input's root
entity is the root of its non-literal operands; an input made only of literals is refused, and
two different roots stay ``PREDICATE_GRAIN_UNSAFE``. An entity with no rows reads exactly what
the arithmetic gives there in the SQL: ``count - 3`` reads -3, not 0, and ``count - 0.1 - 0.2``
reads -0.3, never the float -0.30000000000000004. That reading is known only where the
arithmetic adds, subtracts or multiplies by a constant, over literals the warehouse reads
exactly, so any other input with a literal, a division included, keeps the refusal it had
before literal operands ran.

Gold values come from independent SQL over the same seed. Customer 5 has no orders.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest

from semantic_rails.compiler import _predicate_includes_entities_without_rows
from semantic_rails.compiler_parts.bind import _parse_public_expr
from semantic_rails.compiler_parts.paths import _expression_root_entity
from semantic_rails.errors import SemanticLayerError
from semantic_rails.planner.plan import plan_payload
from semantic_rails.runtime import Runtime

SEED_SQL = """
CREATE TABLE customers (customer_id INTEGER, signed_up_at TIMESTAMP);
CREATE TABLE orders (
  order_id INTEGER, customer_id INTEGER, amount INTEGER, is_returned INTEGER, ordered_at TIMESTAMP
);
CREATE TABLE tickets (ticket_id INTEGER, customer_id INTEGER, opened_at TIMESTAMP);

INSERT INTO customers
SELECT i, TIMESTAMP '2025-01-01 00:00:00' + INTERVAL (i) DAY FROM range(1, 7) t(i);
INSERT INTO orders VALUES
  (1, 1, 100, 0, TIMESTAMP '2025-01-10'), (2, 1, 50, 1, TIMESTAMP '2025-01-20'),
  (3, 1, 30, 0, TIMESTAMP '2025-02-05'), (4, 1, 20, 0, TIMESTAMP '2025-02-15'),
  (5, 2, 70, 1, TIMESTAMP '2025-01-12'), (6, 2, 10, 0, TIMESTAMP '2025-02-02'),
  (7, 3, 10, 0, TIMESTAMP '2025-01-03'), (8, 3, 10, 0, TIMESTAMP '2025-01-04'),
  (9, 3, 10, 0, TIMESTAMP '2025-01-05'), (10, 3, 10, 0, TIMESTAMP '2025-02-06'),
  (11, 3, 10, 0, TIMESTAMP '2025-02-07'),
  (12, 4, 40, 1, TIMESTAMP '2025-02-10'), (13, 4, 40, 1, TIMESTAMP '2025-02-11'),
  (14, 4, 40, 1, TIMESTAMP '2025-02-12');
INSERT INTO orders
SELECT 14 + i, 6, 5, CASE WHEN i = 1 THEN 1 ELSE 0 END,
       TIMESTAMP '2025-01-01' + INTERVAL (i * 5) DAY
FROM range(1, 11) t(i);
INSERT INTO tickets VALUES
  (1, 1, TIMESTAMP '2025-01-11'), (2, 1, TIMESTAMP '2025-02-11'), (3, 4, TIMESTAMP '2025-02-12');
"""

CUSTOMER = "entity.lit_customer"
CUSTOMER_ID = "dimension.lit_customer_id"
ORDERS = {"measure": "measure.lit.order_count"}
REVENUE = {"measure": "measure.lit.revenue"}
TICKETS = {"measure": "measure.lit.ticket_count"}
RETURN_RATE = {"metric": "metric.lit.return_rate"}
INLINE_RATE = {
    "kind": "ratio",
    "numerator": {"measure": "measure.lit.returned_orders"},
    "denominator": ORDERS,
}
ORDER_COUNT_SQL = "coalesce((select count(*) from orders o where o.customer_id = c.customer_id), 0)"
REVENUE_SQL = "(select sum(amount) from orders o where o.customer_id = c.customer_id)"
RATE_SQL = (
    "(select sum(is_returned) / nullif(count(*), 0) from orders o "
    "where o.customer_id = c.customer_id)"
)
FEBRUARY = {
    "temporal_role": "temporal_role.lit_ordered_at",
    "start": "2025-02-01",
    "end": "2025-03-01",
}


def _model(model_id: str, entities: list[str], column: str, measures: str) -> str:
    entity_lines = "\n".join(f"    {name}: {{}}" for name in entities)
    return (
        f"model:\n  id: {model_id}\n  relation: {model_id}\n  entities:\n{entity_lines}\n"
        f"  times:\n    {column}:\n      label: {column}\n      column: {column}\n"
        f"      kind: timestamp\n      class: event_time\n      as: temporal_role.lit_{column}\n"
        f"      default: true\n      default_query_axis: true\n"
        f"  measures:\n{measures}"
    )


def _count(name: str, key: str, accumulation: str = "event") -> str:
    return (
        f"    {name}:\n      label: {name}\n      kind: entity_count\n      entity_key: {key}\n"
        f"      accumulation: {{kind: {accumulation}}}\n      value_type: count\n"
    )


def _sum(name: str, column: str) -> str:
    return (
        f"    {name}:\n      label: {name}\n      kind: aggregate\n      expr: {column}\n"
        "      accumulation: {kind: flow}\n      value_type: count\n"
    )


def _write_package(root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "models").mkdir()
    (root / "metrics").mkdir()
    (root / "data" / "seed.sql").write_text(SEED_SQL)
    (root / "package.yml").write_text(
        textwrap.dedent(
            """
            schema_version: 1
            package:
              id: lit
              namespace: lit
              name: lit
              description: Fixture for literal operands in metric predicates.
              warehouse: duckdb
              default_db: data/lit.duckdb
              seed: {kind: sql_script, source: data/seed.sql}
            """
        )
    )
    (root / "graph.yml").write_text(
        textwrap.dedent(
            """\
            graph:
              entities:
                customer: {label: Customer, key: [customer_id], model: customers}
                order: {label: Order, key: [order_id], model: orders}
                ticket: {label: Ticket, key: [ticket_id], model: tickets}
            """
        )
    )
    models = {
        "customers": _model(
            "customers",
            ["customer"],
            "signed_up_at",
            _count("customer_count", "customer_id", "population"),
        ),
        "orders": _model(
            "orders",
            ["order", "customer"],
            "ordered_at",
            _count("order_count", "order_id")
            + _sum("revenue", "amount")
            + _sum("returned_orders", "is_returned"),
        ),
        "tickets": _model(
            "tickets", ["ticket", "customer"], "opened_at", _count("ticket_count", "ticket_id")
        ),
    }
    for name, text in models.items():
        (root / "models" / f"{name}.yml").write_text(text)
    (root / "metrics" / "core.yml").write_text(
        textwrap.dedent(
            """\
            metrics:
              return_rate:
                label: Return rate
                kind: ratio
                numerator: returned_orders
                denominator: order_count
                value_type: percent
              orders_less_two:
                label: Orders less two
                kind: derived
                value_type: count
                expression:
                  kind: arithmetic
                  op: subtract
                  left: {measure: order_count}
                  right: {kind: literal, value: 2}
              big_spender_orders:
                label: Orders of customers who spent over 100
                kind: aggregate
                value_type: count
                expression:
                  kind: aggregate
                  measure: order_count
                  filter:
                    all:
                      - expression:
                          kind: metric_predicate
                          entity: entity.lit_customer
                          scope_mode: entity_only
                          input:
                            kind: arithmetic
                            op: subtract
                            left: {measure: revenue}
                            right: {kind: literal, value: 100}
                          op: ">"
                          value: 0
            """
        )
    )


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("literal_operands") / "lit"
    _write_package(root)
    runtime = Runtime.from_path(str(root))
    try:
        yield runtime
    finally:
        runtime.close()


def _gold(sql: str) -> list[tuple]:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(SEED_SQL)
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _gold_customers(value_sql: str, condition: str) -> set[int]:
    rows = _gold(
        f"select customer_id from (select c.customer_id, {value_sql} v from customers c) "
        f"where {condition}"
    )
    return {row[0] for row in rows}


def _lit(value) -> dict:
    return {"kind": "literal", "value": value}


def _arith(op: str, left: dict, right: dict) -> dict:
    return {"kind": "arithmetic", "op": op, "left": left, "right": right}


def _predicate(input_: dict, op: str, value, **extra) -> dict:
    return {
        "expression": {
            "kind": "metric_predicate",
            "entity": CUSTOMER,
            "scope_mode": extra.pop("scope_mode", "entity_only"),
            "input": input_,
            "op": op,
            "value": value,
            **extra,
        },
        "op": "=",
        "value": True,
    }


def _customers_query(predicate: dict) -> dict:
    return {
        "version": 1,
        "select": [{"as": "n", "expression": {"measure": "measure.lit.customer_count"}}],
        "group_by": [CUSTOMER_ID],
        "metric_filters": [predicate],
    }


def _kept_customers(runtime: Runtime, predicate: dict) -> set[int]:
    query = _customers_query(predicate)
    assert runtime.validate(query)["ok"] is True
    return {int(row[CUSTOMER_ID]) for row in runtime.query(query)["rows"]}


@pytest.mark.parametrize("rate", [RETURN_RATE, INLINE_RATE], ids=["metric", "inline_ratio"])
@pytest.mark.parametrize(
    "scaled",
    [
        lambda rate: _arith("multiply", rate, _lit(100)),
        lambda rate: _arith("multiply", _lit(100), rate),
    ],
    ids=["literal_right", "literal_left"],
)
def test_a_scaled_rate_keeps_the_customers_the_unscaled_rate_keeps(runtime, rate, scaled):
    expected = _gold_customers(RATE_SQL, "v < 0.3")
    assert expected == {1, 3, 6}
    assert _kept_customers(runtime, _predicate(scaled(rate), "<", 30)) == expected
    assert _kept_customers(runtime, _predicate(rate, "<", 0.3)) == expected


@pytest.mark.parametrize(
    ("literal_form", "plain_form", "condition"),
    [
        # Customer 5 has no orders: it reads 0 - 3, so it passes "< 0" as it passes "< 3".
        ((_arith("subtract", ORDERS, _lit(3)), ">", 0), (ORDERS, ">", 3), "v > 3"),
        ((_arith("subtract", ORDERS, _lit(3)), "<", 0), (ORDERS, "<", 3), "v < 3"),
        ((_arith("subtract", ORDERS, _lit(3)), ">=", -3), (ORDERS, ">=", 0), "v >= 0"),
        ((_arith("add", ORDERS, _lit(5)), "=", 5), (ORDERS, "=", 0), "v = 0"),
        ((_arith("add", _lit(5), ORDERS), ">", 7), (ORDERS, ">", 2), "v > 2"),
        ((_arith("multiply", ORDERS, _lit(100)), "<", 300), (ORDERS, "<", 3), "v < 3"),
        (
            (_arith("multiply", ORDERS, _arith("multiply", _lit(2), _lit(50))), "<", 300),
            (ORDERS, "<", 3),
            "v < 3",
        ),
    ],
    ids=[
        "minus_gt",
        "minus_lt",
        "minus_ge",
        "plus_eq",
        "literal_plus",
        "times_lt",
        "times_nested_literal",
    ],
)
def test_a_count_shifted_by_a_literal_matches_the_unshifted_threshold(
    runtime, literal_form, plain_form, condition
):
    expected = _gold_customers(ORDER_COUNT_SQL, condition)
    assert _kept_customers(runtime, _predicate(*literal_form)) == expected
    assert _kept_customers(runtime, _predicate(*plain_form)) == expected


SHIFTED_DOWN = _arith("subtract", _arith("subtract", ORDERS, _lit(0.1)), _lit(0.2))
SHIFTED_UP = _arith("add", _arith("add", ORDERS, _lit(0.1)), _lit(0.2))


@pytest.mark.parametrize(
    ("input_", "op", "value", "condition", "kept"),
    [
        # In floats customer 5 reads -0.30000000000000004 and is dropped; DuckDB reads -0.3.
        (SHIFTED_DOWN, "=", -0.3, "(v - 0.1) - 0.2 = -0.3", {5}),
        (SHIFTED_DOWN, "NOT IN", [-0.3], "(v - 0.1) - 0.2 not in (-0.3)", {1, 2, 3, 4, 6}),
        # In floats customer 5 reads 0.30000000000000004 and is kept; DuckDB reads 0.3.
        (SHIFTED_UP, ">", 0.3, "(v + 0.1) + 0.2 > 0.3", {1, 2, 3, 4, 6}),
        (SHIFTED_UP, "IN", [0.3, 2.3], "(v + 0.1) + 0.2 in (0.3, 2.3)", {2, 5}),
        (
            _arith("multiply", _arith("add", ORDERS, _lit(0.1)), _lit(3)),
            "=",
            0.3,
            "(v + 0.1) * 3 = 0.3",
            {5},
        ),
    ],
    ids=["minus_eq", "minus_not_in", "plus_gt", "plus_in", "times_eq"],
)
def test_a_decimal_literal_reads_exactly_as_the_sql_does(
    runtime, input_, op, value, condition, kept
):
    expected = _gold_customers(ORDER_COUNT_SQL, condition)
    assert expected == kept
    assert _kept_customers(runtime, _predicate(input_, op, value)) == expected


def test_a_literal_inside_a_metric_recipe_input(runtime):
    # orders_less_two is order_count - 2: customer 5 reads -2.
    expected = _gold_customers(ORDER_COUNT_SQL, "v - 2 < 0")
    assert expected == {5}
    recipe = {"metric": "metric.lit.orders_less_two"}
    assert _kept_customers(runtime, _predicate(recipe, "<", 0)) == expected


def test_a_listed_entity_reading_null_fails_a_threshold_zero_passes(runtime):
    # Customers with no orders qualify here, so the set holds the customers that fail; one the
    # source lists with a NULL value fails too, or it would survive the anti-join.
    query = _customers_query(_predicate(_arith("subtract", ORDERS, _lit(3)), "<", 0))
    sql = runtime.compile(query)["rendered_sql"]
    (failing,) = re.findall(r"WHERE\n(.*__predicate_value.*)", sql)
    assert "__predicate_value >= 0" in failing
    assert "__predicate_value IS NULL" in failing


@pytest.mark.parametrize(
    ("literal_form", "plain_form"),
    [
        ((_arith("multiply", ORDERS, _lit(10)), ">=", 20), (ORDERS, ">=", 2)),
        ((_arith("subtract", ORDERS, _lit(3)), "<", 0), (ORDERS, "<", 3)),
    ],
    ids=["times", "minus_zero_passing"],
)
@pytest.mark.parametrize(
    ("extra", "time"),
    [
        ({}, FEBRUARY),
        ({"scope_mode": "contextual"}, {**FEBRUARY, "grain": "month"}),
        ({"scope_mode": "contextual", "time_grain": "month"}, {**FEBRUARY, "grain": "day"}),
    ],
    ids=["entity_only", "contextual", "anchored_to_the_month"],
)
def test_a_literal_operand_agrees_with_the_plain_form_in_every_scope(
    runtime, literal_form, plain_form, extra, time
):
    def orders(input_, op, value):
        query = {
            "version": 1,
            "select": [{"as": "n", "expression": ORDERS}],
            "group_by": [CUSTOMER_ID],
            "time": time,
            "metric_filters": [_predicate(input_, op, value, **extra)],
        }
        assert runtime.validate(query)["ok"] is True
        rows = runtime.query(query)["rows"]
        return sorted(rows, key=lambda row: sorted((k, str(v)) for k, v in row.items()))

    literal_rows = orders(*literal_form)
    assert literal_rows
    assert literal_rows == orders(*plain_form)


def test_time_anchored_literal_operand_matches_gold(runtime):
    in_february = "ordered_at >= TIMESTAMP '2025-02-01' and ordered_at < TIMESTAMP '2025-03-01'"
    (expected,) = _gold(
        f"select count(*) from orders o where o.{in_february} and o.customer_id in ("
        "select c.customer_id from customers c where (select count(*) from orders x "
        f"where x.customer_id = c.customer_id and x.{in_february}) >= 2)"
    )[0]
    assert expected == 11
    query = {
        "version": 1,
        "select": [{"as": "n", "expression": ORDERS}],
        "time": {**FEBRUARY, "grain": "day"},
        "metric_filters": [
            _predicate(
                _arith("multiply", ORDERS, _lit(10)),
                ">=",
                20,
                scope_mode="contextual",
                time_grain="month",
            )
        ],
    }
    assert sum(row["n"] for row in runtime.query(query)["rows"]) == expected


def _big_spender_orders_gold() -> int:
    (expected,) = _gold(
        "select count(*) from orders o where o.customer_id in (select customer_id from orders "
        "group by customer_id having sum(amount) - 100 > 0)"
    )[0]
    return expected


def test_a_literal_operand_inside_an_aggregate_filter(runtime):
    shifted = _arith("subtract", REVENUE, _lit(100))
    filtered = {
        "kind": "aggregate",
        "measure": "measure.lit.order_count",
        "filter": {"all": [{"expression": _predicate(shifted, ">", 0)["expression"]}]},
    }
    rows = runtime.query({"version": 1, "select": [{"as": "n", "expression": filtered}]})["rows"]
    assert [row["n"] for row in rows] == [_big_spender_orders_gold()] == [7]


def test_a_literal_operand_inside_a_metric_recipe(runtime):
    query = {
        "version": 1,
        "select": [{"as": "n", "expression": {"metric": "metric.lit.big_spender_orders"}}],
    }
    assert runtime.validate(query)["ok"] is True
    assert [row["n"] for row in runtime.query(query)["rows"]] == [_big_spender_orders_gold()]


def _refusal_codes(runtime: Runtime, predicate: dict) -> tuple[list[str], str, str]:
    """What validate, plan and execute each report for one query."""
    query = _customers_query(predicate)
    report = runtime.validate(query)
    assert report["ok"] is False
    plan = plan_payload(runtime, intent="customer count by customer", partial_query=query)
    assert plan["best"]["validation_ok"] is False
    plan_code = plan["why"]["errors"][0]["code"]
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(query)
    return [error["code"] for error in report["errors"]], plan_code, raised.value.code


@pytest.mark.parametrize(
    "input_",
    [
        _arith("multiply", _lit(2), _lit(3)),
        _arith("add", _arith("multiply", _lit(2), _lit(3)), _lit(1)),
    ],
    ids=["literals", "nested_literals"],
)
def test_an_input_made_only_of_literals_is_refused(runtime, input_):
    codes, plan_code, execute_code = _refusal_codes(runtime, _predicate(input_, ">", 1))
    assert codes[0] == plan_code == execute_code == "PREDICATE_INPUT_REQUIRED"


def _ratio(numerator: dict, denominator: dict) -> dict:
    return {"kind": "ratio", "numerator": numerator, "denominator": denominator}


PLUS_ONE = _arith("add", ORDERS, _lit(1))
LESS_TWO = _arith("subtract", ORDERS, _lit(2))


@pytest.mark.parametrize(
    ("input_", "op", "value"),
    [
        # A ratio or comparison with a literal operand stays unsupported, as before.
        (_ratio(ORDERS, _lit(100)), "<", 0.03),
        (_ratio(ORDERS, _lit(2)), "<", 2),
        (_ratio(_lit(1), _lit(2)), ">", 1),
        (_arith("multiply", ORDERS, _ratio(_lit(1), _lit(2))), "<", 1),
        ({"kind": "comparison", "op": "<", "left": ORDERS, "right": _lit(3)}, "=", 1),
        # Gold keeps customer 5, which reads 1 there; no product of two counts is modelled.
        (_arith("multiply", PLUS_ONE, PLUS_ONE), "<", 4),
        (_arith("add", _arith("multiply", ORDERS, ORDERS), _lit(1)), "<", 4),
        # A divisor with a literal: customer 2 divides by 0 and reads NULL, customer 5 by -2.
        (_arith("divide", _lit(5), LESS_TWO), "<", 0),
        (_arith("divide", _lit(5), LESS_TWO), "!=", 1),
        (_arith("divide", _lit(5), {"metric": "metric.lit.orders_less_two"}), "<", 0),
        (_arith("divide", _lit(5), ORDERS), ">", 1),
        # Any division with a literal, even by a constant, as before.
        (_arith("divide", ORDERS, _lit(2)), "<", 2),
        (_arith("divide", REVENUE, _lit(1000)), ">", 0.1),
        (_arith("divide", ORDERS, _lit(0)), "=", 0),
        (
            _arith("add", {"kind": "call", "name": "COALESCE", "args": [ORDERS, _lit(0)]}, _lit(1)),
            "<",
            2,
        ),
        (_arith("add", ORDERS, _lit("3")), "<", 4),
        # A sum may be a float column, whose arithmetic reads 0 - 0.1 as a float.
        (_arith("subtract", REVENUE, _lit(0.1)), "<", 0),
        # DuckDB reads 1e-05 as a float, so neither the literal nor the comparison is exact.
        (_arith("add", ORDERS, _lit(1e-05)), ">", 0),
        (_arith("subtract", ORDERS, _lit(0.1)), "<", 1e-05),
    ],
    ids=[
        "ratio_by_a_literal",
        "ratio_by_two",
        "literal_ratio",
        "times_a_literal_ratio",
        "comparison_with_a_literal",
        "product_of_shifted_counts",
        "product_of_counts_plus_one",
        "literal_over_a_shifted_count",
        "literal_over_a_shifted_count_not_one",
        "literal_over_a_recipe_with_a_literal",
        "literal_over_a_count",
        "count_by_two",
        "sum_by_a_thousand",
        "count_by_zero",
        "call_plus_one",
        "text_literal",
        "decimal_over_a_sum",
        "float_literal",
        "float_threshold",
    ],
)
def test_a_literal_whose_reading_without_rows_is_unknown_is_refused(runtime, input_, op, value):
    predicate = _predicate(input_, op, value)
    codes, plan_code, execute_code = _refusal_codes(runtime, predicate)
    assert codes[0] == plan_code == execute_code == "PREDICATE_NOT_SUPPORTED"
    errors = runtime.validate(_customers_query(predicate))["errors"]
    assert errors[0]["message"] == LITERAL_REFUSAL


LITERAL_REFUSAL = "Expression kind 'literal' is not supported for predicate planning"


@pytest.mark.parametrize("warehouse", ["bigquery", "clickhouse"])
def test_a_decimal_literal_is_refused_where_the_warehouse_reads_a_float(runtime, warehouse):
    config = runtime._config
    floats = replace(config, package=replace(config.package, warehouse=warehouse))
    whole = _parse_public_expr(_arith("subtract", ORDERS, _lit(3)))
    expected_root = _expression_root_entity(whole, config, literal_operands=True)
    assert _expression_root_entity(whole, floats, literal_operands=True) == expected_root
    decimal = _parse_public_expr(_predicate(SHIFTED_DOWN, "=", -0.3)["expression"])
    for refused in (
        lambda: _expression_root_entity(decimal.input, floats, literal_operands=True),
        # A path that reads the value for no rows before it resolves roots is refused too.
        lambda: _predicate_includes_entities_without_rows(decimal, floats),
    ):
        with pytest.raises(SemanticLayerError) as raised:
            refused()
        assert (raised.value.code, str(raised.value)) == (
            "PREDICATE_NOT_SUPPORTED",
            LITERAL_REFUSAL,
        )


@pytest.mark.parametrize(
    "input_",
    [
        _arith("add", ORDERS, TICKETS),
        _arith("add", _arith("multiply", ORDERS, _lit(2)), TICKETS),
        _arith("subtract", _lit(10), _arith("add", TICKETS, _arith("multiply", _lit(2), ORDERS))),
    ],
    ids=["two_roots", "two_roots_with_literal", "two_roots_nested"],
)
def test_two_different_roots_stay_grain_unsafe(runtime, input_):
    codes, plan_code, execute_code = _refusal_codes(runtime, _predicate(input_, ">", 1))
    assert codes[0] == plan_code == execute_code == "PREDICATE_GRAIN_UNSAFE"


def test_plan_accepts_what_execute_answers(runtime):
    predicate = _predicate(_arith("multiply", RETURN_RATE, _lit(100)), "<", 30)
    plan = plan_payload(
        runtime, intent="customer count by customer", partial_query=_customers_query(predicate)
    )
    assert plan["status"] == "ok"
    assert plan["best"]["validation_ok"] is True
    assert predicate in plan["best"]["query_ir"]["metric_filters"]
    assert _kept_customers(runtime, predicate) == {1, 3, 6}
