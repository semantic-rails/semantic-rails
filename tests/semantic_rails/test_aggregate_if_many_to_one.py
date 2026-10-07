"""aggregate_if across many-to-one paths.

An aggregate_if aggregates at the grain of its value's entity; every other column it reads
must be reachable from that entity by one unambiguous chain of declared many-to-one hops.
Anything else is refused with ``UNSUPPORTED_CONDITIONAL_AGGREGATE``. A value row with no match
on the path never satisfies the condition, and a condition such a row could satisfy is
refused, so whether the lookup joins LEFT or INNER changes no value, and each result equals
the filtered-measure leaf that asks the same question.

Fixture: consumption rows of customers. Row 6's customer has no currency, row 7's customer
has no record, row 8 has no customer and row 9 no amount; customer C5 has no rows. Each
consumption row has at most one receipt (one-to-one): row 7 has one, rows 4, 6, 8 and 9 none,
and receipt 99 has no row. Posts of
users, with an owner and an editor (the path preference pins the owner): post 15's owner has
no record and user 3 no age. Order lines, two hops from their customer: line 4's order has a
customer with no record and line 6 an order with no record. Gold values come from SQL
written independently of the engine: scalar subqueries read the looked-up column, NULL where
no row matches, with no joins.
"""

from __future__ import annotations

import dataclasses
import re
import textwrap
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import bind_query, compile_query, plan_query
from semantic_rails.config import load_package_config
from semantic_rails.dialects import _WAREHOUSE_CONNECTORS
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import opened

SEED_SQL = """
CREATE TABLE customers (customer_id VARCHAR, currency VARCHAR, segment VARCHAR);
INSERT INTO customers VALUES ('C1', 'EUR', 'SME'), ('C2', 'CZK', 'LAM'), ('C3', 'EUR', 'KAM'),
  ('C4', NULL, 'SME'), ('C5', 'CZK', 'KAM');
CREATE TABLE segment_history (customer_id VARCHAR, segment VARCHAR, valid_from DATE,
  valid_to DATE);
INSERT INTO segment_history VALUES ('C1', 'LAM', DATE '2025-01-01', DATE '2026-01-01'),
  ('C1', 'SME', DATE '2026-01-01', NULL);
CREATE TABLE consumption (row_id INTEGER, customer_id VARCHAR, amount DECIMAL(10, 2),
  period VARCHAR);
INSERT INTO consumption VALUES
  (1, 'C1', 10, '2026-01'), (2, 'C1', 20, '2026-02'), (3, 'C2', 5, '2026-01'),
  (4, 'C2', 7, '2026-02'), (5, 'C3', 100, '2026-01'), (6, 'C4', 50, '2026-01'),
  (7, 'C9', 1000, '2026-02'), (8, NULL, 3, '2026-02'), (9, 'C3', NULL, '2026-02');
CREATE TABLE users (user_id INTEGER, age INTEGER);
INSERT INTO users VALUES (1, 70), (2, 30), (3, NULL), (4, 66);
CREATE TABLE posts (post_id INTEGER, owner_user_id INTEGER, editor_user_id INTEGER,
  topic_code VARCHAR, view_count INTEGER);
INSERT INTO posts VALUES (10, 1, 2, 'db', 100), (11, 1, 1, 'db', 50), (12, 2, 1, 'ml', 10),
  (13, 3, 3, 'ml', 30), (14, 4, 2, 'db', 70), (15, 9, 1, 'db', 5);
CREATE TABLE topics (topic_id INTEGER, topic_code VARCHAR, topic_name VARCHAR);
INSERT INTO topics VALUES (1, 'db', 'Databases'), (2, 'db', 'Datenbanken'), (3, 'ml', 'ML');
CREATE TABLE tags (tag_id INTEGER, tag_name VARCHAR);
INSERT INTO tags VALUES (1, 'sql'), (2, 'r');
CREATE TABLE post_tags (post_id INTEGER, tag_id INTEGER);
INSERT INTO post_tags VALUES (10, 1), (10, 2), (11, 1), (12, 2);
CREATE TABLE orders (order_id INTEGER, customer_id VARCHAR);
INSERT INTO orders VALUES (100, 'C1'), (101, 'C2'), (102, 'C9'), (103, 'C3');
CREATE TABLE order_lines (line_id INTEGER, order_id INTEGER, quantity INTEGER);
INSERT INTO order_lines VALUES (1, 100, 2), (2, 100, 1), (3, 101, 3), (4, 102, 1), (5, 103, 4),
  (6, 999, 1);
CREATE TABLE receipts (row_id INTEGER, channel VARCHAR);
INSERT INTO receipts VALUES (1, 'web'), (2, 'shop'), (3, 'web'), (5, 'web'), (7, 'web'),
  (99, 'web');
"""

PACKAGE = """
schema_version: 1
package:
  id: shop
  namespace: shop
  warehouse: duckdb
  default_db: data/shop.duckdb
  seed: {kind: sql_script, source: data/seed.sql}
defaults:
  dimension: {groupable: true, filterable: true}
"""

GRAPH = """
graph:
  entities:
    customer: {label: Customer, key: [customer_id], model: customers}
    segment_history: {label: Segment history, key: [customer_id, valid_from],
      model: segment_history}
    consumption: {label: Consumption, key: [row_id], model: consumption}
    user: {label: User, key: [user_id], model: users}
    post: {label: Post, key: [post_id], model: posts}
    topic: {label: Topic, key: [topic_id], model: topics}
    tag: {label: Tag, key: [tag_id], model: tags}
    post_tag: {label: Post tag, key: [post_id, tag_id], model: post_tags}
    order: {label: Order, key: [order_id], model: orders}
    order_line: {label: Order line, key: [line_id], model: order_lines}
    receipt: {label: Receipt, key: [row_id], model: receipts}
  relationships:
    post_owner:
      id: relationship.post_owner
      entities: [post, user]
      cardinality: many_to_one
      via: [owner_user_id]
      target: [user_id]
    post_editor:
      id: relationship.post_editor
      entities: [post, user]
      cardinality: many_to_one
      via: [editor_user_id]
      target: [user_id]
    post_topic:
      id: relationship.post_topic
      entities: [post, topic]
      cardinality: many_to_many
      via: [topic_code]
      target: [topic_code]
    consumption_segment_history:
      id: relationship.consumption_segment_history
      entities: [consumption, segment_history]
      cardinality: many_to_one
      via: [customer_id]
      target: [customer_id]
      temporal_validity:
        valid_from: segment_history.valid_from
        valid_to: segment_history.valid_to
    consumption_receipt:
      id: relationship.consumption_receipt
      entities: [consumption, receipt]
      cardinality: one_to_one
      via: [row_id]
      target: [row_id]
"""


def _pin(relationship: str) -> str:
    return (
        "  path_preferences:\n"
        "    - source_entity: post\n"
        "      target_entity: user\n"
        f"      relationship_path: [{relationship}]\n"
    )


MODELS = {
    "customers": """
        model:
          id: customers
          relation: customers
          entities: {customer: {}}
          dimensions:
            currency: {label: Currency, kind: categorical}
            segment: {label: Segment, kind: categorical}
        """,
    "segment_history": """
        model:
          id: segment_history
          relation: segment_history
          entities: {segment_history: {}}
          dimensions:
            segment: {label: Historic segment, kind: categorical}
        """,
    "consumption": """
        model:
          id: consumption
          relation: consumption
          entities: {consumption: {}, customer: {}}
          dimensions:
            period: {label: Period, kind: categorical}
          measures:
            amount: {label: Amount, kind: aggregate, expr: amount, default_agg: sum,
              accumulation: {kind: flow}}
            consumption_count: {label: Consumption rows, kind: entity_count,
              entity_key: row_id, accumulation: {kind: event}}
            consuming_customers: {label: Consuming customers, kind: entity_count,
              entity_key: customer_id, accumulation: {kind: event}}
            eur_amount:
              label: Amount in euros
              kind: aggregate
              default_agg: sum
              accumulation: {kind: flow}
              expr:
                kind: case
                whens:
                  - when:
                      kind: comparison
                      op: "="
                      left: {kind: column, entity: entity.shop_customer, column: currency}
                      right: {kind: literal, value: EUR}
                    then: {kind: column, column: amount}
            null_currency_amount:
              label: Amount with no customer currency
              kind: aggregate
              default_agg: sum
              accumulation: {kind: flow}
              expr:
                kind: case
                whens:
                  - when:
                      kind: comparison
                      op: IS
                      left: {kind: column, entity: entity.shop_customer, column: currency}
                      right: {kind: literal, value: null}
                    then: {kind: column, column: amount}
        """,
    "users": """
        model:
          id: users
          relation: users
          entities: {user: {}}
          dimensions:
            age: {label: Age, kind: integer}
        """,
    "posts": """
        model:
          id: posts
          relation: posts
          entities: {post: {}}
          measures:
            post_count: {label: Posts, kind: entity_count, entity_key: post_id,
              accumulation: {kind: event}}
            view_count: {label: Views, kind: aggregate, expr: view_count, default_agg: sum,
              accumulation: {kind: flow}}
        """,
    "topics": """
        model:
          id: topics
          relation: topics
          entities: {topic: {}}
          dimensions:
            topic_name: {label: Topic, kind: categorical}
        """,
    "tags": """
        model:
          id: tags
          relation: tags
          entities: {tag: {}}
          dimensions:
            tag_name: {label: Tag, kind: categorical}
        """,
    "post_tags": """
        model:
          id: post_tags
          relation: post_tags
          entities: {post_tag: {}, post: {}, tag: {}}
        """,
    "orders": """
        model:
          id: orders
          relation: orders
          entities: {order: {}, customer: {}}
        """,
    "order_lines": """
        model:
          id: order_lines
          relation: order_lines
          entities: {order_line: {}, order: {}}
          measures:
            line_count: {label: Order lines, kind: entity_count, entity_key: line_id,
              accumulation: {kind: event}}
            quantity: {label: Quantity, kind: aggregate, expr: quantity, default_agg: sum,
              accumulation: {kind: flow}}
            eur_quantity:
              label: Quantity for euro customers
              kind: aggregate
              default_agg: sum
              accumulation: {kind: flow}
              expr:
                kind: case
                whens:
                  - when:
                      kind: comparison
                      op: "="
                      left: {kind: column, entity: entity.shop_customer, column: currency}
                      right: {kind: literal, value: EUR}
                    then: {kind: column, column: quantity}
        """,
    "receipts": """
        model:
          id: receipts
          relation: receipts
          entities: {receipt: {}}
          dimensions:
            channel: {label: Channel, kind: categorical}
        """,
}


def _col(entity: str, column: str) -> dict[str, Any]:
    return {"kind": "column", "entity": f"entity.shop_{entity}", "column": column}


def _lit(value: Any) -> dict[str, Any]:
    return {"kind": "literal", "value": value}


def _cmp(left: dict[str, Any], op: str, right: Any) -> dict[str, Any]:
    return {"kind": "comparison", "op": op, "left": left, "right": _lit(right)}


def _aggif(aggregation: str, condition: dict[str, Any], value: Any = None) -> dict[str, Any]:
    expression = {"kind": "aggregate_if", "aggregation": aggregation, "condition": condition}
    if value is not None:
        expression["value"] = value
    return expression


EUR = _cmp(_col("customer", "currency"), "=", "EUR")
CZK = _cmp(_col("customer", "currency"), "=", "CZK")
NO_CURRENCY = _cmp(_col("customer", "currency"), "IS", None)
OVER_65 = _cmp(_col("user", "age"), ">", 65)
AMOUNT = _col("consumption", "amount")
SUM_EUR = _aggif("sum", EUR, AMOUNT)
OWN_PERIOD = _cmp(_col("consumption", "period"), "=", "2026-01")
SUM_PERIOD = _aggif("sum", OWN_PERIOD, AMOUNT)
CURRENCY = "dimension.shop_customer_currency"
SEGMENT = "dimension.shop_customer_segment"
PERIOD = "dimension.shop_consumption_period"
AGE = "dimension.shop_user_age"

# Scalar subqueries: NULL where the lookup finds no row, independently of any join.
SQL_CUR = "(SELECT c.currency FROM customers AS c WHERE c.customer_id = t.customer_id)"
SQL_SEG = "(SELECT c.segment FROM customers AS c WHERE c.customer_id = t.customer_id)"
SQL_CHANNEL = "(SELECT r.channel FROM receipts AS r WHERE r.row_id = t.row_id)"
SQL_LINE_CUR = (
    "(SELECT c.currency FROM orders AS o, customers AS c"
    " WHERE o.order_id = l.order_id AND c.customer_id = o.customer_id)"
)


def _sql_age(role: str) -> str:
    return f"(SELECT u.age FROM users AS u WHERE u.user_id = p.{role}_user_id)"


def _write_package(
    root: Path, *, pin: str = "relationship.post_owner", seed: str = SEED_SQL, **files: Any
) -> Path:
    pkg = root / "shop"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(seed)
    (pkg / "package.yml").write_text(PACKAGE)
    (pkg / "graph.yml").write_text(GRAPH + (_pin(pin) if pin else ""))
    for name, body in MODELS.items():
        (pkg / "models" / f"{name}.yml").write_text(textwrap.dedent(body))
    for name, doc in files.items():
        (pkg / f"{name}.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return pkg


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    recipe = {"as": "metric.shop.euro_share", "kind": "derived", "expression": SUM_EUR}
    filtered = {
        "as": "metric.shop.filtered_amount",
        "kind": "aggregate",
        "expression": {
            "kind": "aggregate",
            "measure": "measure.shop.amount",
            "aggregation": "sum",
            "filter": {"all": [{"field": PERIOD, "op": "=", "value": "2026-01"}]},
        },
    }
    return _write_package(
        tmp_path_factory.mktemp("aggif"),
        metrics={"metrics": {"shop.euro_share": recipe, "shop.filtered_amount": filtered}},
    )


@pytest.fixture(scope="module")
def runtime(package: Path):
    runtime = Runtime.from_path(str(package))
    yield opened(runtime)
    runtime.close()


@pytest.fixture(scope="module")
def gold():
    connection = duckdb.connect()
    connection.execute(SEED_SQL)

    def run(sql: str) -> dict[Any, float | None]:
        rows = connection.execute(sql).fetchall()
        return {row[0] if len(row) > 1 else None: _number(row[-1]) for row in rows}

    yield run
    connection.close()


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _ask(
    runtime: Runtime, expression: dict[str, Any], *, group_by: str = "", **extra: Any
) -> dict[Any, float | None]:
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"as": "value", "expression": expression}],
        **extra,
    }
    if group_by:
        query["group_by"] = [group_by]
    return {
        row.get(group_by) if group_by else None: _number(row["value"])
        for row in runtime.query(query)["rows"]
    }


def _measure(name: str, aggregation: str = "") -> dict[str, Any]:
    if aggregation:
        return {"kind": "aggregate", "measure": f"measure.shop.{name}", "aggregation": aggregation}
    return {"measure": f"measure.shop.{name}"}


# (aggregate_if, gold SQL, the filtered-measure leaf asking the same question, its where)
ONE_HOP = {
    "sum": (
        SUM_EUR,
        f"SELECT SUM(CASE WHEN {SQL_CUR} = 'EUR' THEN t.amount END) FROM consumption AS t",
        _measure("amount"),
        {"field": CURRENCY, "op": "=", "value": "EUR"},
    ),
    "count": (
        _aggif("count", EUR, _col("consumption", "row_id")),
        f"SELECT COUNT(CASE WHEN {SQL_CUR} = 'EUR' THEN t.row_id END) FROM consumption AS t",
        _measure("consumption_count"),
        {"field": CURRENCY, "op": "=", "value": "EUR"},
    ),
    "avg": (
        _aggif("avg", EUR, AMOUNT),
        f"SELECT AVG(CASE WHEN {SQL_CUR} = 'EUR' THEN t.amount END) FROM consumption AS t",
        _measure("amount", "avg"),
        {"field": CURRENCY, "op": "=", "value": "EUR"},
    ),
    "count_distinct": (
        _aggif("count_distinct", EUR, _col("consumption", "customer_id")),
        "SELECT COUNT(DISTINCT CASE WHEN "
        f"{SQL_CUR} = 'EUR' THEN t.customer_id END) FROM consumption AS t",
        _measure("consuming_customers"),
        {"field": CURRENCY, "op": "=", "value": "EUR"},
    ),
    "one_to_one": (
        _aggif("sum", _cmp(_col("receipt", "channel"), "=", "web"), AMOUNT),
        f"SELECT SUM(CASE WHEN {SQL_CHANNEL} = 'web' THEN t.amount END) FROM consumption AS t",
        _measure("amount"),
        {"field": "dimension.shop_receipt_channel", "op": "=", "value": "web"},
    ),
    "posts_of_owners_over_65": (
        _aggif("count", OVER_65, _col("post", "post_id")),
        f"SELECT COUNT(CASE WHEN {_sql_age('owner')} > 65 THEN p.post_id END) FROM posts AS p",
        _measure("post_count"),
        {"field": AGE, "op": ">", "value": 65},
    ),
    "views_of_owners_over_65": (
        _aggif("sum", OVER_65, _col("post", "view_count")),
        f"SELECT SUM(CASE WHEN {_sql_age('owner')} > 65 THEN p.view_count END) FROM posts AS p",
        _measure("view_count"),
        {"field": AGE, "op": ">", "value": 65},
    ),
    "two_hops": (
        _aggif("sum", EUR, _col("order_line", "quantity")),
        f"SELECT SUM(CASE WHEN {SQL_LINE_CUR} = 'EUR' THEN l.quantity END) FROM order_lines AS l",
        _measure("quantity"),
        {"field": CURRENCY, "op": "=", "value": "EUR"},
    ),
    "two_hops_count": (
        _aggif("count", EUR, _col("order_line", "line_id")),
        f"SELECT COUNT(CASE WHEN {SQL_LINE_CUR} = 'EUR' THEN l.line_id END) FROM order_lines AS l",
        _measure("line_count"),
        {"field": CURRENCY, "op": "=", "value": "EUR"},
    ),
}


@pytest.mark.parametrize("case", sorted(ONE_HOP))
def test_a_condition_across_many_to_one_hops_matches_gold_and_the_filtered_leaf(
    runtime, gold, case
):
    expression, gold_sql, leaf, where = ONE_HOP[case]

    value = _ask(runtime, expression)

    assert value == pytest.approx(gold(gold_sql))
    assert value == pytest.approx(_ask(runtime, leaf, where=[where]))


# A metric filter that every consumption row passes. Query-time filters are contextual: its set
# is matched on the query's grouped entities too.
EVERY_ROW = {
    "expression": {
        "kind": "metric_predicate",
        "entity": "entity.shop_consumption",
        "input": _measure("consumption_count"),
        "op": ">",
        "value": 0,
    },
    "op": "=",
    "value": True,
}
EVERY_ROW_ALONE = {
    **EVERY_ROW,
    "expression": {**EVERY_ROW["expression"], "scope_mode": "entity_only"},
}
# A March row of a customer with no record (C9): it has no currency, so March reads 0.
MARCH_ROW = "INSERT INTO consumption VALUES (10, 'C9', 8, '2026-03');\n"


@pytest.fixture(scope="module")
def march_runtime(tmp_path_factory: pytest.TempPathFactory):
    package = _write_package(tmp_path_factory.mktemp("march"), seed=SEED_SQL + MARCH_ROW)
    runtime = Runtime.from_path(str(package))
    yield opened(runtime)
    runtime.close()


@pytest.mark.parametrize("group_by", ["", PERIOD, SEGMENT], ids=["total", "base", "one"])
@pytest.mark.parametrize(
    "case", sorted(case for case, row in ONE_HOP.items() if "FROM consumption" in row[1])
)
def test_a_metric_filter_every_row_passes_changes_no_value(march_runtime, case, group_by):
    """The filter keeps the condition's lookup LEFT, so every row stays: March, whose one row
    has no customer record, reads 0 (NULL for an average) with the filter as without it. Its
    set is matched on the grouped customer, though, so grouped by the customer, the rows with
    none (7, 8 and March's) have no set to be in; the filter on the rows alone keeps them."""
    expression = ONE_HOP[case][0]

    plain = _ask(march_runtime, expression, group_by=group_by)
    alone = _ask(march_runtime, expression, group_by=group_by, metric_filters=[EVERY_ROW_ALONE])
    contextual = _ask(march_runtime, expression, group_by=group_by, metric_filters=[EVERY_ROW])

    assert alone == plain
    if group_by == PERIOD:
        assert plain["2026-03"] == (None if case == "avg" else 0.0)
    if group_by == SEGMENT:
        assert None in plain
        del plain[None]
    assert contextual == plain


def _in(kind: str, values: list[Any]) -> dict[str, Any]:
    return {"kind": kind, "expr": _col("customer", "currency"), "values": values}


# Conditions on a consumption row's customer: (condition, refused because a row with no
# customer could satisfy it).
CONDITION_SHAPES = {
    "in": (_in("in", ["EUR", "CZK"]), False),
    "not_in": (_in("not_in", ["CZK"]), False),
    "between": (
        {
            "kind": "between",
            "expr": _col("customer", "segment"),
            "low": _lit("KAM"),
            "high": _lit("LAM"),
        },
        False,
    ),
    "column_on_the_right": (
        {
            "kind": "comparison",
            "op": "=",
            "left": _lit("EUR"),
            "right": _col("customer", "currency"),
        },
        False,
    ),
    "is_not_null": (_cmp(_col("customer", "currency"), "IS NOT", None), False),
    "or_inside_an_and": (
        {
            "kind": "boolean",
            "op": "and",
            "args": [
                {"kind": "boolean", "op": "or", "args": [EUR, _cmp(AMOUNT, ">", 500)]},
                _cmp(_col("customer", "currency"), "IS NOT", None),
            ],
        },
        False,
    ),
    "equals_null": (_cmp(_col("customer", "currency"), "=", None), True),
    "is_not_a_value": (_cmp(_col("customer", "currency"), "IS NOT", "EUR"), True),
    "not": ({"kind": "boolean", "op": "not", "args": [CZK]}, True),
    "in_with_null": (_in("in", ["EUR", None]), True),
    "inside_a_call": (
        {
            "kind": "comparison",
            "op": "=",
            "left": {
                "kind": "call",
                "name": "COALESCE",
                "args": [_col("customer", "currency"), _lit("EUR")],
            },
            "right": _lit("EUR"),
        },
        True,
    ),
}


@pytest.mark.parametrize("case", sorted(CONDITION_SHAPES))
def test_a_condition_a_row_with_no_match_could_satisfy_is_refused(runtime, case):
    condition, refused = CONDITION_SHAPES[case]
    expression = _aggif("sum", condition, AMOUNT)

    if not refused:
        assert _ask(runtime, expression, metric_filters=[EVERY_ROW]) == pytest.approx(
            _ask(runtime, expression)
        )
        return
    with pytest.raises(SemanticLayerError) as raised:
        _ask(runtime, expression)
    assert raised.value.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"
    assert raised.value.details["reason"] == "null_accepting_condition"
    assert raised.value.details["entity"] == "entity.shop_customer"


def test_arithmetic_and_ratio_of_conditional_aggregates(runtime, gold):
    difference = {
        "kind": "arithmetic",
        "op": "subtract",
        "left": SUM_EUR,
        "right": _aggif("sum", CZK, AMOUNT),
    }
    ratio = {"kind": "ratio", "numerator": SUM_EUR, "denominator": _measure("amount")}

    assert _ask(runtime, difference) == pytest.approx(
        gold(
            f"SELECT SUM(CASE WHEN {SQL_CUR} = 'EUR' THEN t.amount END)"
            f" - SUM(CASE WHEN {SQL_CUR} = 'CZK' THEN t.amount END) FROM consumption AS t"
        )
    )
    assert _ask(runtime, ratio) == pytest.approx(
        gold(
            f"SELECT SUM(CASE WHEN {SQL_CUR} = 'EUR' THEN t.amount END) / SUM(t.amount)"
            " FROM consumption AS t"
        )
    )


@pytest.mark.parametrize(
    ("group_by", "group_sql"), [(PERIOD, "t.period"), (SEGMENT, SQL_SEG)], ids=["base", "one"]
)
def test_grouped_by_a_base_or_a_one_side_dimension(runtime, gold, group_by, group_sql):
    # Every group of the value's rows is kept; one with no match settles to 0 as today.
    value = _ask(runtime, SUM_EUR, group_by=group_by)
    leaf = _ask(runtime, _measure("amount"), group_by=group_by, where=[ONE_HOP["sum"][3]])

    assert value == pytest.approx(
        gold(
            f"SELECT {group_sql}, COALESCE(SUM(CASE WHEN {SQL_CUR} = 'EUR' THEN t.amount END), 0)"
            " FROM consumption AS t GROUP BY 1"
        )
    )
    assert value == pytest.approx({**dict.fromkeys(value, 0.0), **leaf})
    if group_by == SEGMENT:
        assert value[None] == 0.0  # rows 7 and 8 group under NULL


@pytest.mark.parametrize(
    ("pin", "role"),
    [("relationship.post_owner", "owner"), ("relationship.post_editor", "editor")],
)
def test_the_path_preference_picks_the_role(tmp_path, gold, pin, role):
    runtime = Runtime.from_path(str(_write_package(tmp_path, pin=pin)))
    try:
        expression = _aggif("sum", OVER_65, _col("post", "view_count"))
        plan = plan_query(runtime.config, None, {"select": [{"as": "v", "expression": expression}]})
        value = _ask(runtime, expression)
    finally:
        runtime.close()

    [selection] = plan.measure_plans[0].path_selections
    assert (selection.purpose, selection.chosen_path) == ("aggregate_if", [pin])
    assert value == pytest.approx(
        gold(f"SELECT SUM(CASE WHEN {_sql_age(role)} > 65 THEN p.view_count END) FROM posts AS p")
    )


# (aggregate_if, details["reason"], words the message must name)
REFUSALS = {
    "is_null_on_the_one_side": (
        _aggif("sum", NO_CURRENCY, AMOUNT),
        "null_accepting_condition",
        ["entity.shop_consumption", "entity.shop_customer"],
    ),
    "or_with_the_value_entity": (
        _aggif(
            "count",
            {"kind": "boolean", "op": "or", "args": [EUR, _cmp(AMOUNT, ">", 500)]},
            _col("consumption", "row_id"),
        ),
        "null_accepting_condition",
        ["entity.shop_consumption", "entity.shop_customer"],
    ),
    "condition_on_the_many_side": (
        _aggif("count_distinct", _cmp(AMOUNT, ">", 10), _col("customer", "customer_id")),
        "fanout_hop",
        ["entity.shop_customer", "entity.shop_consumption", "1:N"],
    ),
    "bridge": (
        _aggif("count", _cmp(_col("tag", "tag_name"), "=", "r"), _col("post", "post_id")),
        "fanout_hop",
        ["entity.shop_post", "entity.shop_post_tag", "1:N"],
    ),
    "many_to_many": (
        _aggif("count", _cmp(_col("topic", "topic_name"), "=", "ML"), _col("post", "post_id")),
        "fanout_hop",
        ["relationship.post_topic", "entity.shop_topic", "M:N"],
    ),
    "valid_over_time": (
        _aggif("sum", _cmp(_col("segment_history", "segment"), "=", "SME"), AMOUNT),
        "fanout_hop",
        ["relationship.consumption_segment_history", "valid over time"],
    ),
    "count_without_value_across_entities": (
        _aggif("count", {"kind": "boolean", "op": "and", "args": [EUR, _cmp(AMOUNT, ">", 5)]}),
        "ambiguous_grain",
        ["entity.shop_consumption", "entity.shop_customer"],
    ),
    "value_across_entities": (
        _aggif(
            "sum",
            EUR,
            {"kind": "arithmetic", "op": "add", "left": AMOUNT, "right": _col("user", "age")},
        ),
        "value_spans_entities",
        ["entity.shop_consumption", "entity.shop_user"],
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_other_path_is_refused_with_the_failing_hop_and_a_hint(runtime, case):
    expression, reason, named = REFUSALS[case]

    with pytest.raises(SemanticLayerError) as raised:
        plan_query(runtime.config, None, {"select": [{"as": "v", "expression": expression}]})

    error = raised.value
    assert error.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"
    assert error.details["reason"] == reason
    assert error.details["hint"]
    assert all(word in str(error) for word in named), str(error)


def test_two_roles_with_no_preference_are_refused_never_picked(tmp_path):
    config = load_package_config(str(_write_package(tmp_path, pin="")))
    expression = _aggif("count", OVER_65, _col("post", "post_id"))

    with pytest.raises(SemanticLayerError) as raised:
        plan_query(config, None, {"select": [{"as": "v", "expression": expression}]})

    error = raised.value
    assert error.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"
    assert error.details["reason"] == "ambiguous_path"
    assert sorted(
        option["relationship_path"] for option in error.details["clarification"]["options"]
    ) == [["relationship.post_editor"], ["relationship.post_owner"]]
    assert "path_preferences" in error.details["hint"]


def _predicate(expression: dict[str, Any]) -> dict[str, Any]:
    """A metric filter keeping the customers whose ``expression`` is over 15."""
    return {
        "expression": {
            "kind": "metric_predicate",
            "entity": "entity.shop_customer",
            "input": expression,
            "op": ">",
            "value": 15,
        },
        "op": "=",
        "value": True,
    }


def test_inside_a_metric_predicate_it_computes_gold_or_refuses(runtime, gold):
    # The amount of customers whose euro amount is over 15 (C1: 30, C3: 100).
    value = _ask(runtime, _measure("amount"), metric_filters=[_predicate(SUM_EUR)])
    assert value == pytest.approx(
        gold(
            "SELECT SUM(t.amount) FROM consumption AS t WHERE t.customer_id IN ("
            f" SELECT t.customer_id FROM consumption AS t GROUP BY 1"
            f" HAVING SUM(CASE WHEN {SQL_CUR} = 'EUR' THEN t.amount END) > 15)"
        )
    )

    many_side = REFUSALS["condition_on_the_many_side"][0]
    with pytest.raises(SemanticLayerError) as raised:
        _ask(runtime, _measure("amount"), metric_filters=[_predicate(many_side)])
    assert raised.value.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"


def test_inside_a_metric_recipe_it_is_refused(runtime):
    with pytest.raises(SemanticLayerError) as raised:
        _ask(runtime, {"metric": "metric.shop.euro_share"})

    assert raised.value.code == "INVALID_QUERY"
    assert "aggregate_if" in str(raised.value)


@pytest.mark.parametrize("aggif_first", [False, True], ids=["authored_first", "aggif_first"])
def test_beside_an_authored_measure_on_the_same_hop_each_keeps_its_own_join(
    package, gold, aggif_first
):
    # The authored measure reads the customer through a lookup that keeps its rows, so rows 7
    # and 8 (no customer) have no currency and count, as `where currency IS NULL` counts them.
    # Named with its own table as its source, as a fact model names it and as the
    # aggregate_if's measure is, it still never shares one scan with it.
    config = load_package_config(str(package))
    authored = "measure.shop.null_currency_amount"
    measures = [
        dataclasses.replace(measure, source_relation="consumption")
        if measure.id == authored
        else measure
        for measure in config.measures
    ]
    runtime = Runtime.from_config(
        dataclasses.replace(config, measures=measures), source_path=str(package)
    )
    select = [
        {"as": "authored", "expression": _measure("null_currency_amount")},
        {"as": "value", "expression": SUM_EUR},
    ]
    try:
        alone = _ask(runtime, _measure("null_currency_amount"))
        query = {"version": 1, "select": select[::-1] if aggif_first else select}
        [row] = runtime.query(query)["rows"]
    finally:
        runtime.close()

    null_currency = gold(
        f"SELECT SUM(CASE WHEN {SQL_CUR} IS NULL THEN t.amount END) FROM consumption AS t"
    )
    assert alone == null_currency == {None: 1053.0}  # rows 6, 7 and 8
    assert _number(row["authored"]) == 1053.0
    assert {None: _number(row["value"])} == pytest.approx(gold(ONE_HOP["sum"][1]))


@pytest.mark.parametrize(
    "expression", [_measure("eur_quantity"), ONE_HOP["two_hops"][0]], ids=["authored", "aggif"]
)
def test_a_measure_reading_an_entity_two_hops_away_aggregates_after_its_joins(
    runtime, gold, expression
):
    query = {"version": 1, "select": [{"as": "value", "expression": expression}]}
    performance = compile_query(runtime.config, None, query)["explain"].performance_plan

    assert _ask(runtime, expression) == pytest.approx(gold(ONE_HOP["two_hops"][1]))
    assert performance["joins_after_aggregate"] == []
    assert [(row["strategy"], row["reason"]) for row in performance["joins_before_aggregate"]] == [
        ("direct_aggregate", "measure_expression_reads_joined_entity")
    ]


def test_a_row_filter_on_the_one_side_applies_as_it_does_to_a_where_filter(tmp_path):
    policy = {
        "id": "policy.shop.own_segment",
        "kind": "row_filter",
        "dimension": SEGMENT,
        "attribute": "segment",
        "audiences": ["customer"],
    }
    runtime = Runtime.from_path(
        str(_write_package(tmp_path, policies={"semantic_policies": [policy]}))
    )
    context = RequestContext(actor="end-user", audience="customer", attributes={"segment": "SME"})

    def ask(expression: dict[str, Any], **extra: Any) -> Any:
        return _ask(runtime, expression, policy_context=context.to_policy_context(), **extra)

    try:
        reasons = set()
        for call in (
            lambda: ask(SUM_EUR),
            lambda: ask(_measure("amount"), where=[ONE_HOP["sum"][3]]),
        ):
            with pytest.raises(SemanticLayerError) as raised:
                call()
            assert raised.value.code == "POLICY_DENIED"
            reasons.add(raised.value.details["reason"])
        # An aggregate_if on the filtered entity alone reads only its own rows (C1, C4).
        customers = ask(_aggif("count", _cmp(_col("customer", "currency"), "IS", None)))
    finally:
        runtime.close()

    assert reasons == {"row_filter_unsupported_query"}
    assert customers == {None: 1.0}


@pytest.mark.parametrize("allowed", [False, True])
def test_the_condition_is_a_cut_on_the_entity_it_reads(package, allowed):
    config = load_package_config(str(package))
    entities = ["entity.shop_consumption", *(["entity.shop_customer"] if allowed else [])]
    policy = SemanticPolicyConfig(
        id="policy.shop.filter_entities",
        kind="metric_constraint",
        object_ids=[],
        config={"allowed_metric_filter_entities": entities},
    )
    runtime = Runtime.from_config(
        dataclasses.replace(config, semantic_policies=[policy]), source_path=str(package)
    )
    query = {"version": 1, "select": [{"as": "value", "expression": SUM_EUR}]}
    try:
        result = runtime.validate(query)
    finally:
        runtime.close()

    assert any("entity.shop_customer" in cut for cut in bind_query(config, None, query).cuts)
    if allowed:
        assert result["ok"]
    else:
        assert result["errors"][0]["code"] == "POLICY_DENIED"
        assert result["policy_effects"][0]["violations"][0]["disallowed"] == [
            "entity.shop_customer"
        ]


# The policy kind that declares each action refusing a query by the objects it reads.
POLICY_KINDS = {"deny": "object_access", "redact": "object_access", "hidden": "object_visibility"}
# A hidden dimension read through its column: refused, and neither it nor its policy is named.
NOTHING_NAMED = {"blocked_objects": [], "policy_effects": [], "policy_violations": []}

# Queries whose aggregate_if reads the customer's currency across a hop.
READS_CURRENCY = {
    "select": {"select": [{"as": "value", "expression": SUM_EUR}]},
    "metric_predicate": {
        "select": [{"as": "value", "expression": _measure("amount")}],
        "metric_filters": [_predicate(SUM_EUR)],
    },
    "two_hops": {"select": [{"as": "value", "expression": ONE_HOP["two_hops"][0]}]},
}
RESTRICTED = {"audience": "restricted"}


def _policy(action: str, object_id: str) -> SemanticPolicyConfig:
    return SemanticPolicyConfig(
        id=f"policy.shop.{action}",
        kind=POLICY_KINDS[action],
        object_ids=[object_id],
        audiences=["restricted"],
        action=action,
    )


def _governed(package: Path, *policies: SemanticPolicyConfig, without: str = "") -> Runtime:
    config = load_package_config(str(package))
    return Runtime.from_config(
        dataclasses.replace(
            config,
            dimensions=[row for row in config.dimensions if row.id != without],
            semantic_policies=list(policies),
        ),
        source_path=str(package),
    )


def _refusals(
    runtime: Runtime, monkeypatch, query: dict[str, Any], *, code: str = "POLICY_DENIED"
) -> list[SemanticLayerError]:
    """validate, compile and query each refuse, before the renderer or the adapter runs."""

    def no_output(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the renderer or the adapter was reached before the refusal")

    monkeypatch.setattr("semantic_rails.compiler.render_select_for_profile", no_output)
    monkeypatch.setattr(runtime, "_compile", no_output)
    monkeypatch.setattr(runtime, "_get_adapter", no_output)
    assert runtime.validate(query)["errors"][0]["code"] == code
    refusals = []
    for operation in (runtime.compile, runtime.query):
        with pytest.raises(SemanticLayerError) as raised:
            operation(query)
        assert raised.value.code == code
        refusals.append(raised.value)
    return refusals


@pytest.mark.parametrize("placement", sorted(READS_CURRENCY))
@pytest.mark.parametrize("action", sorted(POLICY_KINDS))
def test_a_policy_on_a_dimension_the_condition_reads_refuses_it(
    package, monkeypatch, action, placement
):
    # The condition reads the dimension's column, as a where filter on it would.
    query = {"version": 1, **READS_CURRENCY[placement]}
    runtime = _governed(package, _policy(action, CURRENCY))
    try:
        allowed = {**query, "policy_context": {"audience": "internal"}}
        assert runtime.validate(allowed)["ok"]
        assert runtime.compile(allowed)["rendered_sql"]
        refusals = _refusals(runtime, monkeypatch, {**query, "policy_context": RESTRICTED})
    finally:
        runtime.close()

    for error in refusals:
        assert error.code == "POLICY_DENIED"
        if action == "hidden":
            assert error.details == NOTHING_NAMED
            continue
        assert error.details["blocked_objects"] == [CURRENCY]
        assert [row["action"] for row in error.details["policy_effects"]] == [action]


def test_a_column_no_dimension_declares_is_refused_under_an_object_policy(package, monkeypatch):
    # With no dimension on the currency column, no policy can name what the condition reads.
    query = {"version": 1, "select": [{"as": "value", "expression": SUM_EUR}]}
    ungoverned = _governed(package, without=CURRENCY)
    governed = _governed(package, _policy("deny", PERIOD), without=CURRENCY)
    try:
        assert ungoverned.compile(query)["rendered_sql"]
        refusals = _refusals(governed, monkeypatch, query)
    finally:
        ungoverned.close()
        governed.close()

    for error in refusals:
        assert error.code == "POLICY_DENIED"
        assert error.details["reason"] == "column_without_dimension"
        assert (error.details["entity"], error.details["column"]) == (
            "entity.shop_customer",
            "currency",
        )


# Each query reads the period, including through wrappers that could hide its dependency.
READS_OWN_PERIOD = {
    "select": ({"select": [{"as": "value", "expression": SUM_PERIOD}]}, 165),
    "arithmetic": (
        {
            "select": [
                {
                    "as": "value",
                    "expression": {
                        "kind": "arithmetic",
                        "op": "add",
                        "left": SUM_PERIOD,
                        "right": _lit(1),
                    },
                }
            ]
        },
        166,
    ),
    "count": (
        {"select": [{"as": "value", "expression": _aggif("count", OWN_PERIOD)}]},
        4,
    ),
    "metric_filters": (
        {
            "select": [{"as": "value", "expression": _measure("amount")}],
            "metric_filters": [{"expression": SUM_PERIOD, "op": ">", "value": 0}],
        },
        1195,
    ),
    "metric_predicate": (
        {
            "select": [{"as": "value", "expression": _measure("amount")}],
            "metric_filters": [_predicate(SUM_PERIOD)],
        },
        150,
    ),
    "authored_filter": (
        {"select": [{"as": "value", "expression": {"metric": "metric.shop.filtered_amount"}}]},
        165,
    ),
}


@pytest.mark.parametrize("placement", sorted(READS_OWN_PERIOD))
@pytest.mark.parametrize("action", sorted(POLICY_KINDS))
def test_a_policy_on_a_dimension_a_single_entity_condition_reads_refuses_it(
    package, monkeypatch, action, placement
):
    body, expected = READS_OWN_PERIOD[placement]
    query = {"version": 1, **body}
    runtime = _governed(package, _policy(action, PERIOD))
    try:
        allowed = {**query, "policy_context": {"audience": "internal"}}
        assert runtime.validate(allowed)["ok"]
        assert runtime.compile(allowed)["rendered_sql"]
        [row] = runtime.query(allowed)["rows"]
        assert float(row["value"]) == expected
        refusals = _refusals(runtime, monkeypatch, {**query, "policy_context": RESTRICTED})
    finally:
        runtime.close()

    for error in refusals:
        if action == "hidden":
            assert error.details == NOTHING_NAMED
            continue
        assert error.details["blocked_objects"] == [PERIOD]
        assert [row["action"] for row in error.details["policy_effects"]] == [action]


@pytest.mark.parametrize("read", ["condition", "value"])
@pytest.mark.parametrize("wrapper", ["column", "call", "cast", "table"])
@pytest.mark.parametrize("action", sorted(POLICY_KINDS))
def test_every_dimension_on_an_own_column_is_bound(package, monkeypatch, action, wrapper, read):
    config = load_package_config(str(package))
    period = next(row for row in config.dimensions if row.id == PERIOD)
    # Two dimensions declare the same column; a policy on either must govern the read.
    dimension = dataclasses.replace(
        period,
        id="dimension.shop_consumption_guarded",
        column="period" if read == "condition" else "amount",
    )
    column = _col("consumption", dimension.column)
    if wrapper == "call":
        column = {"kind": "call", "name": "COALESCE", "args": [column, _lit(None)]}
    elif wrapper == "cast":
        column = {
            "kind": "call",
            "name": "CAST",
            "args": [column, _lit("VARCHAR" if read == "condition" else "DOUBLE")],
        }
    elif wrapper == "table":
        column = {"kind": "column", "table": "consumption", "column": dimension.column.upper()}
    expression = _aggif(
        "sum",
        _cmp(column, "=", "2026-01") if read == "condition" else _cmp(AMOUNT, ">", 0),
        AMOUNT if read == "condition" else column,
    )
    query = {"select": [{"as": "value", "expression": expression}]}
    runtime = Runtime.from_config(
        dataclasses.replace(
            config,
            dimensions=[*config.dimensions, dimension],
            semantic_policies=[_policy(action, dimension.id)],
        ),
        source_path=str(package),
    )
    try:
        allowed = {**query, "policy_context": {"audience": "internal"}}
        assert dimension.id in bind_query(runtime.config, None, allowed).object_ids
        if read == "condition":
            assert PERIOD in bind_query(runtime.config, None, allowed).object_ids
        [row] = runtime.query(allowed)["rows"]
        assert float(row["value"]) == (165 if read == "condition" else 1195)
        refusals = _refusals(runtime, monkeypatch, {**query, "policy_context": RESTRICTED})
    finally:
        runtime.close()
    if action == "hidden":
        assert all(error.details == NOTHING_NAMED for error in refusals)
    else:
        assert all(error.details["blocked_objects"] == [dimension.id] for error in refusals)


@pytest.mark.parametrize("without", ["", PERIOD])
def test_an_own_column_with_no_governed_dimension_remains_allowed(package, without):
    runtime = _governed(package, _policy("deny", CURRENCY), without=without)
    try:
        assert _ask(runtime, SUM_PERIOD, policy_context=RESTRICTED) == {None: 165}
        assert _ask(
            runtime, _aggif("sum", _cmp(AMOUNT, ">", 0), AMOUNT), policy_context=RESTRICTED
        ) == {None: 1195}
    finally:
        runtime.close()


@pytest.mark.parametrize("placement", ["derived", "distribution", "authored_measure"])
@pytest.mark.parametrize("action", sorted(POLICY_KINDS))
def test_unsupported_authored_and_distribution_conditions_cannot_read_columns(
    tmp_path, monkeypatch, action, placement
):
    # These placements have no aggregate_if lowering; keep their existing refusal.
    package = _write_package(
        tmp_path,
        metrics={
            "metrics": {
                "shop.conditional": {
                    "as": "metric.shop.conditional",
                    "kind": "derived",
                    "expression": SUM_PERIOD,
                }
            }
        },
    )
    model_path = package / "models" / "consumption.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["measures"]["conditional_measure"] = {
        "kind": "aggregate",
        "expr": SUM_PERIOD,
        "default_agg": "sum",
        "accumulation": {"kind": "flow"},
    }
    model_path.write_text(yaml.safe_dump(model))
    expression = {
        "derived": {"metric": "metric.shop.conditional"},
        "distribution": {
            "kind": "distribution",
            "function": "avg",
            "over": {
                "kind": "entity_value",
                "entity": "entity.shop_consumption",
                "input": SUM_PERIOD,
            },
        },
        "authored_measure": _measure("conditional_measure"),
    }[placement]
    query = {"select": [{"as": "value", "expression": expression}]}
    code = "INVALID_EXPRESSION_AST" if placement == "authored_measure" else "INVALID_QUERY"
    runtime = _governed(package, _policy(action, PERIOD))
    try:
        for audience in ("internal", "restricted"):
            refusals = _refusals(
                runtime, monkeypatch, {**query, "policy_context": {"audience": audience}}, code=code
            )
            assert all("aggregate_if" in str(error) for error in refusals)
    finally:
        runtime.close()


# The lookup join type (ClickHouse keeps lookups inner, as for a where filter) and the
# value-less count on one entity, which keeps its native form where a dialect has one.
DIALECTS = {
    "athena": ("LEFT", "COUNT(CASE WHEN"),
    "bigquery": ("LEFT", "COUNTIF("),
    "clickhouse": ("INNER", "COUNT(CASE WHEN"),
    "databricks": ("LEFT", "COUNT_IF("),
    "duckdb": ("LEFT", "COUNT(CASE WHEN"),
    "ducklake": ("LEFT", "COUNT(CASE WHEN"),
    "motherduck": ("LEFT", "COUNT(CASE WHEN"),
    "postgres": ("LEFT", "COUNT(CASE WHEN"),
    "snowflake": ("LEFT", "COUNT_IF("),
}


def test_every_warehouse_has_a_rendering():
    assert set(DIALECTS) == set(_WAREHOUSE_CONNECTORS)


@pytest.mark.parametrize("warehouse", sorted(DIALECTS))
def test_the_rendered_sql_per_dialect(package, warehouse):
    base = load_package_config(str(package))
    config = dataclasses.replace(
        base, package=dataclasses.replace(base.package, warehouse=warehouse)
    )
    query = {
        "version": 1,
        "select": [
            {"as": "eur", "expression": SUM_EUR},
            {"as": "large", "expression": _aggif("count", _cmp(AMOUNT, ">", 10))},
        ],
    }

    sql = " ".join(compile_query(config, Registry(config), query)["sql"].split())

    join_type, count_if = DIALECTS[warehouse]
    assert re.findall(r"\b(\w+) JOIN customers\b", sql) == [join_type]
    assert re.search(
        r"SUM\(CASE WHEN customers\.currency = 'EUR' THEN consumption\.amount END\)", sql
    )
    assert "SUM_IF(" not in sql
    assert count_if in sql
