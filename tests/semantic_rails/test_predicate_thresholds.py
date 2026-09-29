"""metric_predicate thresholds that zero satisfies, and predicates on another clock.

An entity with no rows never reaches a predicate's aggregate, so ``count = 0`` or
``< 3`` must count it explicitly: gold values here come from independent SQL over
the same seed. A predicate measured on another clock than the query's is refused
unless it asks for calendar-period alignment.

Seed: customers and members are the qualifying entities. Each has entities with no
rows (customers 4 and 5 have no orders; member 5 has no activity, member 4 has one
activity months after enrolling).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime

SEED_SQL = """
CREATE TABLE customers (customer_id INTEGER, signed_up_at TIMESTAMP);
CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, amount INTEGER, ordered_at TIMESTAMP);
CREATE TABLE members (member_id INTEGER, enrolled_at TIMESTAMP);
CREATE TABLE activities (activity_id INTEGER, member_id INTEGER, minutes INTEGER, active_on TIMESTAMP);
CREATE TABLE tickets (ticket_id INTEGER, member_id INTEGER, opened_on TIMESTAMP);

INSERT INTO customers VALUES
  (1, TIMESTAMP '2025-01-05 00:00:00'), (2, TIMESTAMP '2025-01-20 00:00:00'),
  (3, TIMESTAMP '2025-02-03 00:00:00'), (4, TIMESTAMP '2025-02-10 00:00:00'),
  (5, TIMESTAMP '2025-03-01 00:00:00');
INSERT INTO orders VALUES
  (1, 1, 100, TIMESTAMP '2025-01-10 00:00:00'), (2, 1, 50, TIMESTAMP '2025-02-12 00:00:00'),
  (3, 2, 70, TIMESTAMP '2025-01-15 00:00:00'), (4, 3, 30, TIMESTAMP '2025-02-20 00:00:00'),
  (5, NULL, 40, TIMESTAMP '2025-02-21 00:00:00');
ALTER TABLE orders ADD COLUMN status VARCHAR DEFAULT 'placed';
UPDATE orders SET status = 'returned' WHERE order_id = 3;
UPDATE orders SET status = NULL WHERE order_id = 2;
INSERT INTO members VALUES
  (1, TIMESTAMP '2025-01-01 00:00:00'), (2, TIMESTAMP '2025-01-05 00:00:00'),
  (3, TIMESTAMP '2025-02-01 00:00:00'), (4, TIMESTAMP '2025-02-15 00:00:00'),
  (5, TIMESTAMP '2025-03-01 00:00:00');
INSERT INTO activities VALUES
  (1, 1, 10, TIMESTAMP '2025-01-02 00:00:00'), (2, 1, 20, TIMESTAMP '2025-01-20 00:00:00'),
  (3, 2, 5, TIMESTAMP '2025-01-06 00:00:00'), (4, 3, 40, TIMESTAMP '2025-02-02 00:00:00'),
  (5, 3, 15, TIMESTAMP '2025-02-10 00:00:00'), (6, 1, 12, TIMESTAMP '2025-02-08 00:00:00'),
  (7, 4, 8, TIMESTAMP '2025-03-05 00:00:00');
INSERT INTO tickets VALUES (1, 1, TIMESTAMP '2025-01-03 00:00:00');
"""

CUSTOMER = "entity.pred_customer"
MEMBER = "entity.pred_member"


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("thresholds") / "pred"
    (root / "data").mkdir(parents=True)
    (root / "models").mkdir()
    (root / "data" / "seed.sql").write_text(SEED_SQL)
    (root / "package.yml").write_text(
        textwrap.dedent(
            """
            schema_version: 1
            package:
              id: pred
              namespace: pred
              name: pred
              description: Fixture for thresholds that zero satisfies.
              warehouse: duckdb
              default_db: data/pred.duckdb
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
                member: {label: Member, key: [member_id], model: members}
                activity: {label: Activity, key: [activity_id], model: activities}
                ticket: {label: Ticket, key: [ticket_id], model: tickets}
            """
        )
    )
    _write_models(root / "models")
    runtime = Runtime.from_path(str(root))
    try:
        yield runtime
    finally:
        runtime.close()


def _model(
    model_id: str,
    entities: list[str],
    time: tuple[str, str],
    measures: str,
    dimensions: str = "",
) -> str:
    column, role = time
    entity_lines = "\n".join(f"    {name}: {{}}" for name in entities)
    return (
        f"model:\n  id: {model_id}\n  relation: {model_id}\n  entities:\n{entity_lines}\n"
        f"  times:\n    {column}:\n      label: {column}\n      column: {column}\n"
        f"      kind: timestamp\n      class: event_time\n      as: temporal_role.pred_{role}\n"
        f"      default: true\n      default_query_axis: true\n{dimensions}"
        f"  measures:\n{measures}"
    )


def _count(name: str, key: str, accumulation: str = "event") -> str:
    return (
        f"    {name}:\n      label: {name}\n      kind: entity_count\n      entity_key: {key}\n"
        f"      accumulation: {{kind: {accumulation}}}\n      value_type: count\n"
    )


def _write_models(models: Path) -> None:
    amount = (
        "    revenue:\n      label: Revenue\n      kind: aggregate\n      expr: amount\n"
        "      accumulation: {kind: flow}\n      value_type: count\n"
    )
    minutes = (
        "    minutes:\n      label: Minutes\n      kind: aggregate\n      expr: minutes\n"
        "      accumulation: {kind: flow}\n      value_type: count\n"
    )
    files = {
        "customers": _model(
            "customers",
            ["customer"],
            ("signed_up_at", "signed_up_at"),
            _count("customer_count", "customer_id", "population"),
        ),
        "orders": _model(
            "orders",
            ["order", "customer"],
            ("ordered_at", "ordered_at"),
            _count("order_count", "order_id") + amount,
            "  dimensions:\n    status:\n      as: dimension.pred_status\n"
            "      label: Status\n      kind: categorical\n",
        ),
        "members": _model(
            "members",
            ["member"],
            ("enrolled_at", "enrolled_at"),
            _count("member_count", "member_id", "population"),
        ),
        "activities": _model(
            "activities",
            ["activity", "member"],
            ("active_on", "active_on"),
            _count("activity_count", "activity_id") + minutes,
        ),
        "tickets": _model(
            "tickets",
            ["ticket", "member"],
            ("opened_on", "opened_on"),
            _count("ticket_count", "ticket_id")
            + _count("ticketing_member_count", "member_id", "population"),
        ),
    }
    for name, text in files.items():
        (models / f"{name}.yml").write_text(text)


def _gold(sql: str) -> list[tuple]:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(SEED_SQL)
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _predicate(entity: str, input_: dict, op: str, value, **extra) -> dict:
    return {
        "expression": {
            "kind": "metric_predicate",
            "entity": entity,
            "scope_mode": extra.pop("scope_mode", "entity_only"),
            "input": input_,
            "op": op,
            "value": value,
            **extra,
        },
        "op": "=",
        "value": True,
    }


def _run(runtime: Runtime, measure: str, filters: list[dict], **extra) -> list[dict]:
    result = runtime.query(
        {
            "version": 1,
            "select": [{"as": "n", "expression": {"measure": f"measure.pred.{measure}"}}],
            "metric_filters": filters,
            **extra,
        }
    )
    return result["rows"]


def _scalar(runtime: Runtime, measure: str, filters: list[dict]) -> int:
    (row,) = _run(runtime, measure, filters)
    return int(row["n"])


ORDERS = {"measure": "measure.pred.order_count"}
REVENUE = {"measure": "measure.pred.revenue"}
ACTIVITIES = {"measure": "measure.pred.activity_count"}
RETURNED_ORDERS = {
    "kind": "aggregate",
    "measure": "measure.pred.order_count",
    "filter": {"all": [{"field": "dimension.pred_status", "op": "=", "value": "returned"}]},
}


def test_seed_has_entities_without_rows():
    assert _gold(
        "select count(*) from customers c where not exists (select 1 from orders o where o.customer_id = c.customer_id)"
    ) == [(2,)]
    assert _gold(
        "select count(*) from members m where not exists (select 1 from activities a where a.member_id = m.member_id)"
    ) == [(1,)]


@pytest.mark.parametrize(
    ("op", "value", "gold_predicate"),
    [
        ("=", 0, "n = 0"),
        ("<", 2, "n < 2"),
        ("<", 3, "n < 3"),
        ("<=", 1, "n <= 1"),
        ("<=", 0, "n <= 0"),
        ("!=", 1, "n <> 1"),
        (">=", 0, "n >= 0"),
        ("IN", [0, 2], "n in (0, 2)"),
        ("NOT IN", [1], "n not in (1)"),
        # Thresholds zero does not satisfy keep their meaning: entities with rows only.
        (">", 0, "n > 0"),
        (">=", 2, "n >= 2"),
        ("=", 1, "n = 1"),
    ],
)
def test_customers_by_order_count_include_customers_with_no_orders(
    runtime, op, value, gold_predicate
):
    (expected,) = _gold(
        "select count(*) from (select c.customer_id, "
        "(select count(*) from orders o where o.customer_id = c.customer_id) n "
        f"from customers c) where {gold_predicate}"
    )[0]
    filters = [_predicate(CUSTOMER, ORDERS, op, value)]
    assert _scalar(runtime, "customer_count", filters) == expected


def test_zero_orders_returns_the_customers_that_never_ordered(runtime):
    filters = [_predicate(CUSTOMER, ORDERS, "=", 0)]
    assert _scalar(runtime, "customer_count", filters) == 2


@pytest.mark.parametrize(("op", "value"), [("=", 0), ("<", 100), ("<=", 70)])
def test_sum_thresholds_count_customers_with_no_orders_as_zero(runtime, op, value):
    sql_op = {"=": "=", "<": "<", "<=": "<="}[op]
    (expected,) = _gold(
        "select count(*) from (select c.customer_id, "
        "coalesce((select sum(amount) from orders o where o.customer_id = c.customer_id), 0) n "
        f"from customers c) where n {sql_op} {value}"
    )[0]
    filters = [_predicate(CUSTOMER, REVENUE, op, value)]
    assert _scalar(runtime, "customer_count", filters) == expected


def test_members_with_zero_activity(runtime):
    assert _scalar(runtime, "member_count", [_predicate(MEMBER, ACTIVITIES, "=", 0)]) == 1
    (expected,) = _gold(
        "select count(*) from (select m.member_id, "
        "(select count(*) from activities a where a.member_id = m.member_id) n "
        "from members m) where n < 2"
    )[0]
    assert _scalar(runtime, "member_count", [_predicate(MEMBER, ACTIVITIES, "<", 2)]) == expected


def _net_orders(null_behavior: str | None) -> dict:
    return {
        "kind": "arithmetic",
        "op": "subtract",
        "left": ORDERS,
        "right": RETURNED_ORDERS,
        **({"null_behavior": null_behavior} if null_behavior else {}),
    }


NET_ORDERS_GOLD = (
    "select count(*) from (select c.customer_id, "
    "(select count(*) from orders o where o.customer_id = c.customer_id) "
    "- (select count(*) from orders o where o.customer_id = c.customer_id "
    "and o.status = 'returned') n from customers c) where n {op} {value}"
)


@pytest.mark.parametrize(("op", "value"), [("<", 1), ("=", 0), ("<=", 1), ("!=", 2), (">", 1)])
def test_a_difference_over_two_leaves_counts_each_side_as_zero_when_asked(runtime, op, value):
    # Customer 1 has orders and none returned: its net is 2, not NULL.
    sql_op = "<>" if op == "!=" else op
    (expected,) = _gold(NET_ORDERS_GOLD.format(op=sql_op, value=value))[0]
    filters = [_predicate(CUSTOMER, _net_orders("coalesce_zero"), op, value)]
    assert _scalar(runtime, "customer_count", filters) == expected


CUSTOMER_ORDER_AND_RETURN_COUNTS = (
    "(select c.customer_id, "
    "(select count(*) from orders o where o.customer_id = c.customer_id) o_n, "
    "(select count(*) from orders o where o.customer_id = c.customer_id "
    "and o.status = 'returned') r_n from customers c)"
)


@pytest.mark.parametrize("null_behavior", [None, "", "propagate"])
@pytest.mark.parametrize(("op", "value"), [("<", 1), ("=", 0), ("<=", 1)])
def test_a_difference_over_two_leaves_without_coalesce_zero_keeps_customers_with_both_sides(
    runtime, null_behavior, op, value
):
    # The difference is NULL when either side has no rows, and NULL satisfies no threshold.
    (expected,) = _gold(
        f"select count(*) from {CUSTOMER_ORDER_AND_RETURN_COUNTS} "
        f"where o_n > 0 and r_n > 0 and o_n - r_n {op} {value}"
    )[0]
    filters = [_predicate(CUSTOMER, _net_orders(null_behavior), op, value)]
    assert _scalar(runtime, "customer_count", filters) == expected


def test_a_nested_difference_stays_null_unless_every_level_coalesces(runtime):
    outer = {
        "kind": "arithmetic",
        "op": "add",
        "left": _net_orders(None),
        "right": ORDERS,
        "null_behavior": "coalesce_zero",
    }
    (expected,) = _gold(
        f"select count(*) from {CUSTOMER_ORDER_AND_RETURN_COUNTS} "
        "where o_n > 0 and r_n > 0 and (o_n - r_n) + o_n < 1"
    )[0]
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, outer, "<", 1)]) == expected


def test_an_empty_not_in_list_is_satisfied_by_every_entity(runtime):
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, ORDERS, "NOT IN", [])]) == 5
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, ORDERS, "IN", [])]) == 0


def test_orders_without_a_customer_are_not_customers_with_no_orders(runtime):
    # Order 5 has no customer: it belongs to nobody, whatever the threshold.
    (expected,) = _gold(
        "select count(*) from orders o where o.customer_id in (select c.customer_id "
        "from customers c where coalesce((select sum(amount) from orders x "
        "where x.customer_id = c.customer_id), 0) < 60)"
    )[0]
    filters = [_predicate(CUSTOMER, REVENUE, "<", 60)]
    assert _scalar(runtime, "order_count", filters) == expected == 1


def test_a_fact_measure_restricted_to_entities_with_no_related_rows(runtime):
    # Activities of members who never opened a ticket.
    (expected,) = _gold(
        "select count(*) from activities a "
        "where not exists (select 1 from tickets t where t.member_id = a.member_id)"
    )[0]
    tickets = {"measure": "measure.pred.ticket_count"}
    filters = [_predicate(MEMBER, tickets, "=", 0)]
    assert _scalar(runtime, "activity_count", filters) == expected == 4


def test_a_distinct_count_of_a_population_is_zero_over_no_rows(runtime):
    ticketing_members = {"measure": "measure.pred.ticketing_member_count"}
    filters = [_predicate(MEMBER, ticketing_members, "=", 0)]
    assert _scalar(runtime, "activity_count", filters) == 4


def test_zero_threshold_composes_with_a_second_predicate(runtime):
    # Members with no tickets and fewer than two activities.
    (expected,) = _gold(
        "select count(*) from members m "
        "where not exists (select 1 from tickets t where t.member_id = m.member_id) "
        "and (select count(*) from activities a where a.member_id = m.member_id) < 2"
    )[0]
    filters = [
        _predicate(MEMBER, {"measure": "measure.pred.ticket_count"}, "=", 0),
        _predicate(MEMBER, ACTIVITIES, "<", 2),
    ]
    assert _scalar(runtime, "member_count", filters) == expected


def test_zero_threshold_by_period_keeps_each_period_apart(runtime):
    # Activities per month, counting a member only in months with no ticket of theirs.
    expected = dict(
        _gold(
            "select strftime(date_trunc('month', a.active_on), '%Y-%m'), count(*) "
            "from activities a where not exists (select 1 from tickets t "
            "where t.member_id = a.member_id "
            "and date_trunc('month', t.opened_on) = date_trunc('month', a.active_on)) "
            "group by 1"
        )
    )
    filters = [
        _predicate(
            MEMBER,
            {"measure": "measure.pred.ticket_count"},
            "=",
            0,
            scope_mode="contextual",
            time_alignment="same_query_period",
        )
    ]
    rows = _run(
        runtime,
        "activity_count",
        filters,
        time={"temporal_role": "temporal_role.pred_active_on", "grain": "month"},
    )
    got = {str(row["temporal_role.pred_active_on__month"])[:7]: int(row["n"]) for row in rows}
    assert got == expected == {"2025-01": 1, "2025-02": 3, "2025-03": 1}


def test_zero_threshold_sql_is_an_anti_join(runtime):
    filters = [_predicate(CUSTOMER, ORDERS, "=", 0)]
    compiled = runtime.compile(
        {
            "version": 1,
            "select": [{"as": "n", "expression": {"measure": "measure.pred.customer_count"}}],
            "metric_filters": filters,
        }
    )
    sql = " ".join(compiled["rendered_sql"].upper().split())
    assert "LEFT JOIN" in sql and "IS NULL" in sql


def test_explain_marks_the_predicate_set_as_an_anti_join(runtime):
    def predicate_sets(op, value):
        query = {
            "version": 1,
            "select": [{"as": "n", "expression": {"measure": "measure.pred.customer_count"}}],
            "metric_filters": [_predicate(CUSTOMER, ORDERS, op, value)],
        }
        compiled = compile_query(runtime._config, Registry(runtime._config), query)
        return [row for row in compiled["physical_plan"].nodes if row.kind == "PredicateSet"]

    (zero,) = predicate_sets("=", 0)
    (nonzero,) = predicate_sets(">", 0)
    assert zero.details["anti_join"] is True
    assert "anti_join" not in nonzero.details


@pytest.mark.parametrize("aggregation", ["avg", "min", "max", "median"])
@pytest.mark.parametrize(("op", "value"), [("<", 10), ("<=", 12), ("!=", 5), ("<", 100)])
def test_a_threshold_zero_satisfies_on_an_aggregate_without_an_empty_value_keeps_rows_only(
    runtime, aggregation, op, value
):
    # A member with no activity has a NULL average, minimum, maximum or median, and NULL
    # satisfies no threshold: the answer is the SQL HAVING answer over members with rows.
    minutes = {"measure": "measure.pred.minutes", "aggregation": aggregation}
    sql_op = "<>" if op == "!=" else op
    (expected,) = _gold(
        f"select count(*) from (select {aggregation}(minutes) n from activities "
        f"group by member_id) where n {sql_op} {value}"
    )[0]
    assert _scalar(runtime, "member_count", [_predicate(MEMBER, minutes, op, value)]) == expected


def test_customers_with_an_average_order_value_under_a_limit_are_only_those_with_orders(runtime):
    average_order_value = {"measure": "measure.pred.revenue", "aggregation": "avg"}
    (expected,) = _gold(
        "select count(*) from (select avg(amount) n from orders "
        "where customer_id is not null group by customer_id) where n < 60"
    )[0]
    filters = [_predicate(CUSTOMER, average_order_value, "<", 60)]
    assert _scalar(runtime, "customer_count", filters) == expected == 1


def test_a_ratio_threshold_excludes_entities_whose_denominator_has_no_rows(runtime):
    # Orders per returned order: only customer 2 has a returned order, so only it has a ratio.
    ratio = {"kind": "ratio", "numerator": ORDERS, "denominator": RETURNED_ORDERS}
    (expected,) = _gold(
        "select count(*) from (select "
        "(select count(*) from orders o where o.customer_id = c.customer_id) * 1.0 "
        "/ nullif((select count(*) from orders o where o.customer_id = c.customer_id "
        "and o.status = 'returned'), 0) n from customers c) where n < 5"
    )[0]
    assert (
        _scalar(runtime, "customer_count", [_predicate(CUSTOMER, ratio, "<", 5)]) == expected == 1
    )


@pytest.mark.parametrize(("op", "value"), [(">", 10), (">=", 5), ("=", 12)])
def test_a_threshold_zero_fails_on_such_an_aggregate_is_unchanged(runtime, op, value):
    minutes = {"measure": "measure.pred.minutes", "aggregation": "avg"}
    (expected,) = _gold(
        "select count(*) from (select m.member_id, "
        "(select avg(minutes) from activities a where a.member_id = m.member_id) n "
        f"from members m) where n {op} {value}"
    )[0]
    assert _scalar(runtime, "member_count", [_predicate(MEMBER, minutes, op, value)]) == expected


def test_percentile_thresholds_are_unchanged(runtime):
    threshold = {"kind": "percentile", "p": 0.5}
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, ORDERS, ">=", threshold)]) >= 1


def _by_enrolment_month(runtime: Runtime, predicate: dict) -> list[dict]:
    return _run(
        runtime,
        "member_count",
        [{"expression": predicate, "op": "=", "value": True}],
        time={"temporal_role": "temporal_role.pred_enrolled_at", "grain": "month"},
    )


CROSS_CLOCK = {
    "kind": "metric_predicate",
    "entity": MEMBER,
    "scope_mode": "contextual",
    "input": ACTIVITIES,
    "op": ">=",
    "value": 1,
}


def test_a_contextual_predicate_on_another_clock_is_refused_with_choices(runtime):
    with pytest.raises(SemanticLayerError) as raised:
        _by_enrolment_month(runtime, CROSS_CLOCK)
    error = raised.value
    assert error.code == "INVALID_TEMPORAL_BINDING"
    assert error.details["requested"] == "temporal_role.pred_enrolled_at"
    assert error.details["compatible"] == ["temporal_role.pred_active_on"]
    hint = error.details["recovery_hints"][0]["message"]
    assert "temporal_role.pred_active_on" in hint and "entity_only" in hint


def test_a_predicate_on_the_query_clock_needs_no_alignment(runtime):
    # Activities per month by members with at least two activities that month.
    expected = dict(
        _gold(
            "select strftime(date_trunc('month', a.active_on), '%Y-%m'), count(*) from activities a "
            "where (select count(*) from activities b where b.member_id = a.member_id "
            "and date_trunc('month', b.active_on) = date_trunc('month', a.active_on)) >= 2 "
            "group by 1"
        )
    )
    filters = [{"expression": {**CROSS_CLOCK, "value": 2}, "op": "=", "value": True}]
    rows = _run(
        runtime,
        "activity_count",
        filters,
        time={"temporal_role": "temporal_role.pred_active_on", "grain": "month"},
    )
    got = {str(row["temporal_role.pred_active_on__month"])[:7]: int(row["n"]) for row in rows}
    assert got == expected == {"2025-01": 2, "2025-02": 2}


def test_calendar_alignment_across_clocks_must_be_asked_for(runtime):
    # Members by enrolment month with activity in that same calendar month.
    expected = dict(
        _gold(
            "select strftime(date_trunc('month', m.enrolled_at), '%Y-%m'), count(*) from members m "
            "where exists (select 1 from activities a where a.member_id = m.member_id "
            "and date_trunc('month', a.active_on) = date_trunc('month', m.enrolled_at)) group by 1"
        )
    )
    rows = _by_enrolment_month(runtime, {**CROSS_CLOCK, "time_alignment": "same_query_period"})
    got = {str(row["temporal_role.pred_enrolled_at__month"])[:7]: int(row["n"]) for row in rows}
    assert got == expected == {"2025-01": 2, "2025-02": 1}


def test_entity_only_scope_is_the_lifetime_alternative(runtime):
    rows = _by_enrolment_month(runtime, {**CROSS_CLOCK, "scope_mode": "entity_only"})
    got = {str(row["temporal_role.pred_enrolled_at__month"])[:7]: int(row["n"]) for row in rows}
    assert got == {"2025-01": 2, "2025-02": 2}


def test_a_same_clock_contextual_anti_join_by_a_grouped_dimension_keeps_only_real_keys(runtime):
    # Orders per status and month from customers whose revenue that month is under 60. Order 5
    # has no customer and order 2 no status: neither is a customer "with no orders that month".
    expected = {
        (status, month): count
        for status, month, count in _gold(
            "select o.status, strftime(date_trunc('month', o.ordered_at), '%Y-%m'), count(*) "
            "from orders o where o.customer_id is not null and (select sum(x.amount) from orders x "
            "where x.customer_id = o.customer_id "
            "and date_trunc('month', x.ordered_at) = date_trunc('month', o.ordered_at)) < 60 "
            "group by 1, 2"
        )
    }
    query = {
        "version": 1,
        "select": [{"as": "n", "expression": {"measure": "measure.pred.order_count"}}],
        "metric_filters": [_predicate(CUSTOMER, REVENUE, "<", 60, scope_mode="contextual")],
        "group_by": ["dimension.pred_status"],
        "time": {"temporal_role": "temporal_role.pred_ordered_at", "grain": "month"},
    }
    rows = runtime.query(query)["rows"]
    got = {
        (row["dimension.pred_status"], str(row["temporal_role.pred_ordered_at__month"])[:7]): int(
            row["n"]
        )
        for row in rows
    }
    assert got == expected == {(None, "2025-02"): 1, ("placed", "2025-02"): 1}
    # Every key the anti-join matches on, context and period included, must be present.
    sql = " ".join(runtime.compile(query)["rendered_sql"].split())
    for key in (
        "orders.customer_id",
        "orders.order_id",
        "DATE_TRUNC('month', CAST(orders.ordered_at AS TIMESTAMP))",
    ):
        assert f"AND {key} IS NOT NULL" in sql
