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
CUSTOMER_ID = "dimension.pred_customer_id"
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


def _case_count(name: str, key: str, column: str, floor: int) -> str:
    """A conditional count, like ``COUNT(CASE WHEN column >= floor THEN key END)``: 0, not NULL,
    for an entity whose rows all fall short."""
    return (
        f"    {name}:\n      label: {name}\n      kind: entity_count\n"
        "      accumulation: {kind: event}\n      value_type: count\n"
        "      expr:\n        kind: case\n        whens:\n"
        f"          - when: {{kind: comparison, op: '>=', left: {{kind: column, column: {column}}},"
        f" right: {{kind: literal, value: {floor}}}}}\n"
        f"            then: {{kind: column, column: {key}}}\n"
        "        else: {kind: literal, value: null}\n"
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
            _count("order_count", "order_id")
            + amount
            + _case_count("large_order_count", "order_id", "amount", 100)
            + _case_count("huge_order_count", "order_id", "amount", 1000),
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
            + _count("ticketing_member_count", "member_id", "population")
            + _case_count("late_ticket_count", "ticket_id", "ticket_id", 100),
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


def _scalar(runtime: Runtime, measure: str, filters: list[dict]) -> int | None:
    """The one value, with NULL kept apart from 0."""
    (row,) = _run(runtime, measure, filters)
    return None if row["n"] is None else int(row["n"])


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


def _net_orders() -> dict:
    return {"kind": "arithmetic", "op": "subtract", "left": ORDERS, "right": RETURNED_ORDERS}


NET_ORDERS_GOLD = (
    "select count(*) from (select c.customer_id, "
    "(select count(*) from orders o where o.customer_id = c.customer_id) "
    "- (select count(*) from orders o where o.customer_id = c.customer_id "
    "and o.status = 'returned') n from customers c) where n {op} {value}"
)


@pytest.mark.parametrize(("op", "value"), [("<", 1), ("=", 0), ("<=", 1), ("!=", 2), (">", 1)])
def test_a_difference_over_two_leaves_counts_each_side_as_zero(runtime, op, value):
    # Customer 1 has orders and none returned: its net is 2, not NULL, as in any projection.
    sql_op = "<>" if op == "!=" else op
    (expected,) = _gold(NET_ORDERS_GOLD.format(op=sql_op, value=value))[0]
    filters = [_predicate(CUSTOMER, _net_orders(), op, value)]
    assert _scalar(runtime, "customer_count", filters) == expected


def test_a_nested_difference_settles_every_operand(runtime):
    outer = {"kind": "arithmetic", "op": "add", "left": _net_orders(), "right": ORDERS}
    (expected,) = _gold(
        "select count(*) from (select c.customer_id, "
        "(select count(*) from orders o where o.customer_id = c.customer_id) o_n, "
        "(select count(*) from orders o where o.customer_id = c.customer_id "
        "and o.status = 'returned') r_n from customers c) where (o_n - r_n) + o_n > 2"
    )[0]
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, outer, ">", 2)]) == expected


@pytest.mark.parametrize(("op", "value"), [(">", 1), ("<", 1)])
def test_a_predicate_and_a_projection_of_the_same_difference_agree(runtime, op, value):
    """One rule: the entities a predicate keeps are the ones a metric filter keeps."""
    net = {"kind": "arithmetic", "op": "subtract", "left": ORDERS, "right": RETURNED_ORDERS}
    by_customer = runtime.query(
        {
            "version": 1,
            "select": [{"as": "net", "expression": net}],
            "group_by": [CUSTOMER_ID],
            "metric_filters": [{"expression": net, "op": op, "value": value}],
        }
    )["rows"]
    kept = {row[CUSTOMER_ID] for row in by_customer}
    (row,) = _run(runtime, "customer_count", [_predicate(CUSTOMER, net, op, value)])
    # A projection lists only customers with orders; the predicate also counts those with none.
    with_none = 2 if op == "<" else 0
    assert int(row["n"]) == len(kept) + with_none
    assert all(row["net"] is not None for row in by_customer)


CANCELLED_ORDERS = {
    "kind": "aggregate",
    "measure": "measure.pred.order_count",
    "filter": {"all": [{"field": "dimension.pred_status", "op": "=", "value": "cancelled"}]},
}


@pytest.mark.parametrize(("op", "value"), [("<", 1), ("=", 0), ("<=", 0), ("!=", 2), ("!=", 1)])
def test_an_operand_with_no_data_reads_null_for_every_customer_present_or_absent(
    runtime, op, value
):
    """No order is ever cancelled, so the difference is NULL for every customer.

    Customers 1 to 3 have orders and customers 4 and 5 have none, and all five read the same:
    NULL, which fails every threshold, in a predicate as in a metric filter.
    """
    net = {"kind": "arithmetic", "op": "subtract", "left": ORDERS, "right": CANCELLED_ORDERS}
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, net, op, value)]) == 0
    by_customer = runtime.query(
        {
            "version": 1,
            "select": [{"as": "net", "expression": net}],
            "group_by": [CUSTOMER_ID],
            "metric_filters": [{"expression": net, "op": op, "value": value}],
        }
    )["rows"]
    assert by_customer == []


FEBRUARY = {
    "temporal_role": "temporal_role.pred_ordered_at",
    "start": "2025-02-01",
    "end": "2025-03-01",
}


@pytest.mark.parametrize(("op", "value"), [("=", 0), ("<", 1), ("!=", 2)])
def test_a_window_in_which_an_operand_has_no_data_keeps_no_customer(runtime, op, value):
    """The only return is in January, so in February the difference is NULL for everyone."""
    net = {"kind": "arithmetic", "op": "subtract", "left": ORDERS, "right": RETURNED_ORDERS}
    filters = [_predicate(CUSTOMER, net, op, value, scope_mode="contextual")]
    # Orders 2 and 4 are the February orders of customers 1 and 3, and neither customer is kept.
    # A window with nothing left in it returns no row at all, not a row of NULL.
    assert _run(runtime, "order_count", filters, time=FEBRUARY) == []


def _if_count(entity: str, column: str, floor: int) -> dict:
    """The query-time form of a conditional count: a ``CASE`` with no ``ELSE``, counted."""
    return {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": {
            "kind": "comparison",
            "op": ">=",
            "left": {"kind": "column", "column": column, "entity": entity},
            "right": {"kind": "literal", "value": floor},
        },
    }


# Conditional counts, as a measure and at query time. Order 1 is the only one at or above 100,
# no order reaches 1000, and no ticket is late (ticket_id >= 100).
LARGE_ORDERS = {"measure": "measure.pred.large_order_count"}
HUGE_ORDERS = {"measure": "measure.pred.huge_order_count"}
LATE_TICKETS = {"measure": "measure.pred.late_ticket_count"}
LARGE_ORDER_COUNTS = pytest.mark.parametrize(
    "large",
    [LARGE_ORDERS, _if_count("entity.pred_order", "amount", 100)],
    ids=["case_count", "aggregate_if"],
)
HUGE_ORDER_COUNTS = pytest.mark.parametrize(
    "huge",
    [HUGE_ORDERS, _if_count("entity.pred_order", "amount", 1000)],
    ids=["case_count", "aggregate_if"],
)
LATE_TICKET_COUNTS = pytest.mark.parametrize(
    "late",
    [LATE_TICKETS, _if_count("entity.pred_ticket", "ticket_id", 100)],
    ids=["case_count", "aggregate_if"],
)
ZERO_PASSING = pytest.mark.parametrize(("op", "value"), [("=", 0), ("<", 1), ("<=", 0), ("!=", 1)])


def _no_data_warnings(response: dict) -> list[dict]:
    return [item for item in response["warnings"] if item["code"] == "NO_DATA_IN_SCOPE"]


@LARGE_ORDER_COUNTS
@ZERO_PASSING
def test_a_conditional_count_with_no_match_in_scope_is_unobserved_for_a_customer_with_orders(
    runtime, large, op, value
):
    """Only order 3 is returned, and it is small: customer 2 has an order in scope and a count of 0.

    Nothing matched anywhere in scope, so that 0 is no data. Customer 2 reads NULL, so
    `= 0` selects nobody, and the query says so instead of answering with a confident 0.
    """
    (kept,) = _gold(
        "select count(*) from orders o where o.status = 'returned' and o.customer_id in ("
        "select c.customer_id from customers c where exists (select 1 from orders x "
        "where x.status = 'returned' and x.amount >= 100) and (select count(*) from orders y "
        "where y.customer_id = c.customer_id and y.status = 'returned' and y.amount >= 100) = 0)"
    )[0]
    assert kept == 0
    response = runtime.query(
        {
            "version": 1,
            "select": [{"as": "n", "expression": {"measure": "measure.pred.order_count"}}],
            "where": [{"field": "dimension.pred_status", "op": "=", "value": "returned"}],
            "metric_filters": [_predicate(CUSTOMER, large, op, value, scope_mode="contextual")],
        }
    )
    assert response["rows"] == [{"n": None}]
    (warning,) = _no_data_warnings(response)
    assert warning["details"]["outputs"] == ["n"]
    assert warning["object_ids"] == ["measure.pred.order_count"]


@HUGE_ORDER_COUNTS
@ZERO_PASSING
def test_a_conditional_count_with_no_match_over_all_time_selects_no_customer(
    runtime, huge, op, value
):
    """No order reaches 1000: every customer's count of huge orders is 0, and none is data.

    Customers 1 to 3 have orders and customers 4 and 5 have none. A raw count per customer
    reads 0 for all five, but nothing matched anywhere, so all five read NULL and none is kept.
    """
    (naive,) = _gold(
        "select count(*) from customers c where (select count(*) from orders o "
        "where o.customer_id = c.customer_id and o.amount >= 1000) = 0"
    )[0]
    assert naive == 5
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, huge, op, value)]) == 0


@LATE_TICKET_COUNTS
@ZERO_PASSING
def test_a_conditional_count_with_no_match_in_scope_is_unobserved_for_a_member_with_no_rows(
    runtime, late, op, value
):
    """No ticket is late: member 1 has a ticket and members 2 to 5 have none, and all read NULL.

    Members 2 to 4 would have 4 activities between them if an entity with no rows counted as
    0 while the measure had no data anywhere, which is what one rule for every entity rules out.
    """
    (kept,) = _gold(
        "select count(*) from activities a where a.member_id in (select m.member_id from members m "
        "where exists (select 1 from tickets t where t.ticket_id >= 100) and (select count(*) "
        "from tickets u where u.member_id = m.member_id and u.ticket_id >= 100) = 0)"
    )[0]
    assert kept == 0
    response = runtime.query(
        {
            "version": 1,
            "select": [{"as": "n", "expression": {"measure": "measure.pred.activity_count"}}],
            "metric_filters": [_predicate(MEMBER, late, op, value)],
        }
    )
    assert response["rows"] == [{"n": None}]
    (warning,) = _no_data_warnings(response)
    assert warning["details"]["outputs"] == ["n"]


@LARGE_ORDER_COUNTS
@ZERO_PASSING
def test_a_conditional_count_with_one_match_gives_every_other_customer_zero(
    runtime, large, op, value
):
    """Order 1 is large, so customers 2 and 3 (orders, none large) and 4 and 5 (no orders) read 0."""
    sql_op = "<>" if op == "!=" else op
    (expected,) = _gold(
        "select count(*) from (select c.customer_id, (select count(*) from orders o "
        "where o.customer_id = c.customer_id and o.amount >= 100) n from customers c "
        f"where exists (select 1 from orders x where x.amount >= 100)) where n {sql_op} {value}"
    )[0]
    assert expected == 4
    assert _scalar(runtime, "customer_count", [_predicate(CUSTOMER, large, op, value)]) == expected


def test_a_conditional_count_with_one_match_in_a_period_gives_every_other_customer_zero(runtime):
    """In January order 1 (customer 1) is large and order 3 (customer 2) is not: order 3 is kept."""
    january = {**FEBRUARY, "start": "2025-01-01", "end": "2025-02-01"}
    in_january = "ordered_at >= TIMESTAMP '2025-01-01' and ordered_at < TIMESTAMP '2025-02-01'"
    (expected,) = _gold(
        f"select count(*) from orders o where o.{in_january} and o.customer_id in ("
        "select c.customer_id from customers c where exists (select 1 from orders x "
        f"where x.amount >= 100 and x.{in_january}) and (select count(*) from orders y "
        f"where y.customer_id = c.customer_id and y.amount >= 100 and y.{in_january}) = 0)"
    )[0]
    filters = [_predicate(CUSTOMER, LARGE_ORDERS, "=", 0, scope_mode="contextual")]
    assert expected == 1
    assert [row["n"] for row in _run(runtime, "order_count", filters, time=january)] == [1]


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


@pytest.mark.parametrize(
    "input_",
    [ORDERS, {"measure": "measure.pred.revenue", "aggregation": "avg"}],
    ids=["count", "avg"],
)
@pytest.mark.parametrize("op", ["=", "!="])
def test_a_null_threshold_is_refused(runtime, op, input_):
    # Every customer has a known order count (0 for customers 4 and 5), and those two have no
    # average: a null test on the value would drop both, so the predicate refuses instead.
    with pytest.raises(SemanticLayerError) as exc:
        _run(runtime, "customer_count", [_predicate(CUSTOMER, input_, op, None)])
    assert exc.value.code == "INVALID_METRIC_PREDICATE"
    assert exc.value.details["recovery_hints"][0]["code"] == "USE_ZERO_OR_INPUT_NULL_TEST"


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


@pytest.mark.xfail(
    strict=True,
    reason="An ungrouped empty population reads 0 without a warning: https://github.com/semantic-rails/semantic-rails/issues/203",
)
def test_an_ungrouped_empty_population_should_read_null_with_a_warning(runtime):
    query = {
        "version": 2,
        "select": [{"expression": {"measure": "measure.pred.customer_count"}, "as": "n"}],
        "where": [{"field": CUSTOMER_ID, "op": "=", "value": 999}],
    }
    result = runtime.query(query)
    assert result["rows"] == [{"n": None}] and _no_data_warnings(result)


def test_a_grouped_empty_population_has_no_groups(runtime):
    result = runtime.query(
        {
            "version": 2,
            "select": [{"expression": {"measure": "measure.pred.customer_count"}, "as": "n"}],
            "group_by": [CUSTOMER_ID],
            "where": [{"field": CUSTOMER_ID, "op": "=", "value": 999}],
        }
    )
    assert result["rows"] == []


def test_an_empty_population_predicate_equals_zero_as_documented(runtime):
    empty = {
        "kind": "aggregate",
        "measure": "measure.pred.customer_count",
        "filter": {"all": [{"field": CUSTOMER_ID, "op": "=", "value": 999}]},
    }
    assert _scalar(runtime, "order_count", [_predicate(CUSTOMER, empty, "=", 0)]) == 4


def test_a_group_missing_a_population_leaf_reads_null(runtime):
    result = runtime.query(
        {
            "version": 2,
            "group_by": ["dimension.pred_member_id"],
            "select": [
                {"expression": {"measure": "measure.pred.activity_count"}, "as": "activities"},
                {
                    "expression": {"measure": "measure.pred.ticketing_member_count"},
                    "as": "population",
                },
            ],
        }
    )
    gold = _gold(
        "SELECT a.member_id, COUNT(a.activity_id), MAX(p.n) FROM activities a "
        "LEFT JOIN (SELECT member_id, COUNT(DISTINCT member_id) n FROM tickets GROUP BY 1) p "
        "ON p.member_id=a.member_id GROUP BY 1"
    )
    assert sorted(
        (r["dimension.pred_member_id"], r["activities"], r["population"]) for r in result["rows"]
    ) == sorted(gold)
    assert any(row[2] is None for row in gold)
