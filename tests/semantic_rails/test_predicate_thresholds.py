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

from semantic_rails.errors import SemanticLayerError
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
  (3, 2, 70, TIMESTAMP '2025-01-15 00:00:00'), (4, 3, 30, TIMESTAMP '2025-02-20 00:00:00');
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


def _model(model_id: str, entities: list[str], time: tuple[str, str], measures: str) -> str:
    column, role = time
    entity_lines = "\n".join(f"    {name}: {{}}" for name in entities)
    return (
        f"model:\n  id: {model_id}\n  relation: {model_id}\n  entities:\n{entity_lines}\n"
        f"  times:\n    {column}:\n      label: {column}\n      column: {column}\n"
        f"      kind: timestamp\n      class: event_time\n      as: temporal_role.pred_{role}\n"
        f"      default: true\n      default_query_axis: true\n  measures:\n{measures}"
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
            _count("ticket_count", "ticket_id"),
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


def test_a_fact_measure_restricted_to_entities_with_no_related_rows(runtime):
    # Activities of members who never opened a ticket.
    (expected,) = _gold(
        "select count(*) from activities a "
        "where not exists (select 1 from tickets t where t.member_id = a.member_id)"
    )[0]
    tickets = {"measure": "measure.pred.ticket_count"}
    filters = [_predicate(MEMBER, tickets, "=", 0)]
    assert _scalar(runtime, "activity_count", filters) == expected == 4


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


@pytest.mark.parametrize("aggregation", ["avg", "min", "max", "median"])
def test_a_threshold_zero_satisfies_on_an_aggregate_without_an_empty_value_is_refused(
    runtime, aggregation
):
    minutes = {"measure": "measure.pred.minutes", "aggregation": aggregation}
    with pytest.raises(SemanticLayerError) as raised:
        _run(runtime, "member_count", [_predicate(MEMBER, minutes, "<", 10)])
    assert raised.value.code == "INVALID_METRIC_PREDICATE"
    assert "no rows" in str(raised.value)
    assert raised.value.details["recovery_hints"]


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
