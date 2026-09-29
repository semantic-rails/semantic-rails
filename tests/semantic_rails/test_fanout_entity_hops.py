"""Measures across one-to-many hops count each row of their own entity once.

An order-grain measure filtered or grouped by an item dimension (orders -> order_items) used to
be refused with MIXED_GRAIN_INVALID. The leaf now keeps one row per (order key, output grain)
before it aggregates: filtered, an order counts once at all (EXISTS); grouped, a distinct count
counts it once in every product type it contains ("orders that included it"). Every answer is
checked on DuckDB against reference SQL written independently of the engine.

The item -> order relationship here is inferred, with no ``rollup_safe`` opt-in, so the
entity_in_terms_of shortcut does not apply.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime

# Order 1 has two beverages (the double-count trap), order 2 a beverage and a jaffle, order 3
# two jaffles, order 4 a beverage after Q4 2016, order 5 no items, and order 6 a beverage and a
# NULL total. Two orders with a NULL key and the same total hold beverages with a NULL order
# key: no join matches them, so they count nowhere. Customer 10 has sessions on two channels,
# 11 on one, and 12 none. Coupons join orders on their code, which is not their key.
SEED = """
CREATE TABLE orders AS SELECT * FROM (VALUES
  (1, 10, TIMESTAMP '2016-10-05 10:00:00', 10.0, 'A'),
  (2, 10, TIMESTAMP '2016-11-01 10:00:00', 20.0, 'A'),
  (3, 11, TIMESTAMP '2016-12-01 10:00:00', 30.0, 'B'),
  (4, 12, TIMESTAMP '2017-01-15 10:00:00', 40.0, NULL),
  (5, 12, TIMESTAMP '2016-12-20 10:00:00', 50.0, NULL),
  (6, 11, TIMESTAMP '2016-10-10 10:00:00', NULL, NULL),
  (NULL, 12, TIMESTAMP '2016-10-20 10:00:00', 70.0, NULL),
  (NULL, 12, TIMESTAMP '2016-10-21 10:00:00', 70.0, NULL)
) AS t(order_id, customer_id, ordered_at, total, coupon_code);
CREATE TABLE order_items AS SELECT * FROM (VALUES
  (101, 1, 'coffee', 'beverage', true, 3.0),
  (102, 1, 'tea', 'beverage', true, 4.0),
  (201, 2, 'coffee', 'beverage', true, 3.0),
  (202, 2, 'toast', 'jaffle', false, 17.0),
  (301, 3, 'toast', 'jaffle', false, 15.0),
  (302, 3, 'melt', 'jaffle', true, 15.0),
  (401, 4, 'tea', 'beverage', true, 40.0),
  (601, 6, 'coffee', 'beverage', true, 5.0),
  (701, NULL, 'coffee', 'beverage', true, 2.0),
  (702, NULL, 'tea', 'beverage', true, 2.0)
) AS t(item_id, order_id, sku, product_type, is_hot, revenue);
CREATE TABLE products AS SELECT * FROM (VALUES
  ('coffee', 'hot'), ('tea', 'hot'), ('toast', 'food'), ('melt', 'food')
) AS t(sku, category);
CREATE TABLE customers AS SELECT * FROM (VALUES
  (10, 100.0, 1.5), (11, 200.0, 2.5), (12, 400.0, 3.5)
) AS t(customer_id, credit, score);
CREATE TABLE sessions AS SELECT * FROM (VALUES
  (1, 10, 'web'), (2, 10, 'app'), (3, 11, 'web')
) AS t(session_id, customer_id, channel);
CREATE TABLE coupons AS SELECT * FROM (VALUES (1, 'A', 5.0), (2, 'B', 7.0)) AS t(coupon_id, code, face_value);
"""

PACKAGE = """
schema_version: 1
package: {id: hop, namespace: hop, warehouse: duckdb, default_db: data/warehouse.duckdb,
  seed: {kind: sql_script, source: data/seed.sql}, schema_strict: true}
"""
MODELS = {
    "orders": """
model:
  id: orders
  relation: orders
  entities: {order: {}, customer: {}, coupon: {expr: coupon_code}}
  times:
    ordered_at: {label: Order time, column: ordered_at, kind: timestamp, class: event_time,
      default: true}
  measures:
    order_count: {label: Orders, kind: entity_count, entity_key: order_id,
      accumulation: {kind: event}, value_type: count}
    buyer_count: {label: Buyers, kind: entity_count, entity_key: customer_id,
      accumulation: {kind: event}, value_type: count}
    revenue: {label: Revenue, kind: aggregate, expr: total, accumulation: {kind: flow},
      value_type: currency}
""",
    "order_items": """
model:
  id: order_items
  relation: order_items
  entities: {item: {}, order: {}, product: {expr: sku}}
  dimensions:
    product_type: {label: Item product type, kind: categorical}
    is_hot: {label: Hot item, kind: boolean}
  measures:
    item_revenue: {label: Item revenue, kind: aggregate, expr: revenue,
      accumulation: {kind: flow}, value_type: currency, time: temporal_role.hop_order_ordered_at}
""",
    "products": """
model:
  id: products
  relation: products
  entities: {product: {}}
  dimensions:
    category: {label: Category, kind: categorical}
""",
    "customers": """
model:
  id: customers
  relation: customers
  entities: {customer: {}}
  measures:
    customer_count: {label: Customers, kind: entity_count, entity_key: customer_id,
      accumulation: {kind: event}, value_type: count}
    credit: {label: Credit, kind: aggregate, expr: credit, accumulation: {kind: flow},
      value_type: currency}
    score: {label: Score, kind: aggregate, expr: score, accumulation: {kind: flow},
      additive: false}
""",
    "sessions": """
model:
  id: sessions
  relation: sessions
  entities: {session: {}, customer: {}}
  dimensions:
    channel: {label: Channel, kind: categorical}
""",
    "coupons": """
model:
  id: coupons
  relation: coupons
  entities: {coupon: {}}
  measures:
    face_value: {label: Face value, kind: aggregate, expr: face_value,
      accumulation: {kind: flow}, value_type: currency}
""",
}
ENTITIES = {
    "order": ["order_id", "orders"],
    "item": ["item_id", "order_items"],
    "product": ["sku", "products"],
    "customer": ["customer_id", "customers"],
    "session": ["session_id", "sessions"],
    "coupon": ["coupon_id", "coupons"],
}
# Orders reach a coupon by its code, not its key.
RELATIONSHIPS = {
    "order_coupon": {
        "entities": ["order", "coupon"],
        "cardinality": "many_to_one",
        "target": ["code"],
    }
}

ROLE = "temporal_role.hop_order_ordered_at"
TYPE = "dimension.hop_item_product_type"
HOT = "dimension.hop_item_is_hot"
CATEGORY = "dimension.hop_product_category"
CHANNEL = "dimension.hop_session_channel"
Q4_2016 = {"temporal_role": ROLE, "grain": "quarter", "start": "2016-10-01", "end": "2017-01-01"}
IN_Q4 = "o.ordered_at >= TIMESTAMP '2016-10-01' AND o.ordered_at < TIMESTAMP '2017-01-01'"
BEVERAGE = {"field": TYPE, "op": "=", "value": "beverage"}
FILTERED_SUM = {
    "select": [{"expression": {"measure": "measure.hop.revenue"}, "as": "revenue"}],
    "where": [BEVERAGE],
}
GROUPED_COUNT = {
    "select": [{"expression": {"measure": "measure.hop.order_count"}, "as": "orders"}],
    "group_by": [TYPE],
}


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("hop") / "hop"
    entities = {
        name: {"label": name, "key": [key], "model": model}
        for name, (key, model) in ENTITIES.items()
    }
    graph = {"graph": {"entities": entities, "relationships": RELATIONSHIPS}}
    files = {
        "package.yml": PACKAGE,
        "data/seed.sql": SEED,
        "graph.yml": yaml.safe_dump(graph),
        **{f"models/{name}.yml": text for name, text in MODELS.items()},
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    _run(root, {"select": [_measure("order_count")]})  # seeds the warehouse
    return root


def _measure(name: str, aggregation: str = "", alias: str = "") -> dict[str, Any]:
    expression = {"measure": f"measure.hop.{name}"}
    if aggregation:
        expression["aggregation"] = aggregation
    return {"expression": expression, "as": alias or f"{name}_{aggregation}".rstrip("_")}


def _run(package: Path, query: dict[str, Any], *, validate: bool = False) -> dict[str, Any]:
    engine = Runtime.from_path(str(package))
    try:
        payload = {"version": 1, **query}
        return engine.validate(payload) if validate else engine.query(payload)
    finally:
        engine.close()


def _rows(package: Path, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    result = _run(package, query)
    assert result.get("ok", True), result
    return _normal(tuple(row.values()) for row in result["rows"])


def _reference(package: Path, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        return _normal(conn.execute(sql).fetchall())


def _normal(rows: Any) -> list[tuple[Any, ...]]:
    """Sorted rows with floats rounded, without the one quarter bucket the window pins."""
    return sorted(
        (
            tuple(
                round(float(v), 6) if isinstance(v, (float, Decimal)) else v
                for v in row
                if not hasattr(v, "year")
            )
            for row in rows
        ),
        key=lambda row: [str(v) for v in row],
    )


def _refusal(package: Path, query: dict[str, Any]) -> dict[str, Any]:
    report = _run(package, query, validate=True)
    assert report["ok"] is False, report
    return report["errors"][0]


def _disclosed(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in result["warnings"] if w["details"].get("rewrite_kind") == "fanout_dedup"]


def test_orders_that_included_each_product_type_beside_item_revenue(package: Path) -> None:
    query = {
        "select": [_measure("order_count", "count_distinct"), _measure("item_revenue")],
        "group_by": [TYPE],
        "time": Q4_2016,
    }
    reference = f"""
        SELECT i.product_type, COUNT(DISTINCT i.order_id), SUM(i.revenue)
        FROM order_items i JOIN orders o ON o.order_id = i.order_id WHERE {IN_Q4} GROUP BY 1
    """
    rows = _rows(package, query)
    assert rows == _reference(package, reference)
    # Order 2 holds both types, so it counts under each.
    assert [row[:2] for row in rows] == [("beverage", 3), ("jaffle", 2)]


def test_revenue_of_orders_with_a_beverage_counts_each_order_once(package: Path) -> None:
    """The double-count trap: order 1's two beverages add its total once."""
    query = {**FILTERED_SUM, "time": Q4_2016}
    reference = f"""
        SELECT SUM(o.total) FROM orders o WHERE {IN_Q4} AND EXISTS (
          SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND i.product_type = 'beverage')
    """
    assert _rows(package, query) == _reference(package, reference) == [(30.0,)]
    naive = f"""
        SELECT SUM(o.total) FROM orders o JOIN order_items i ON i.order_id = o.order_id
        WHERE {IN_Q4} AND i.product_type = 'beverage'
    """
    assert _reference(package, naive) == [(40.0,)]  # what a plain join would have said


@pytest.mark.parametrize("query", [FILTERED_SUM, GROUPED_COUNT], ids=["filtered", "grouped"])
def test_the_rewrite_is_disclosed(package: Path, query: dict[str, Any]) -> None:
    result = _run(package, query)
    [warning] = _disclosed(result)
    assert warning["code"] == "REWRITE_APPLIED"
    assert "'entity.hop_order' counts once per group" in warning["message"]
    assert "at least one matching row" in warning["message"]
    assert warning["details"]["paths"] == {"entity.hop_item": ["relationship.order_items_order"]}
    assert result["provenance_summary"]["rewrite_status"] == "rewritten"
    explain = _run(package, query, validate=True)["explain"]
    assert explain["fanout_strategy"]["status"] == "rewritten"


def test_other_aggregations_under_a_filter_read_each_order_once(package: Path) -> None:
    query = {
        "select": [
            _measure("revenue", "avg"),
            _measure("revenue", "max"),
            _measure("revenue", "median"),
            _measure("order_count"),
        ],
        "where": [BEVERAGE],
    }
    reference = """
        SELECT AVG(o.total), MAX(o.total), MEDIAN(o.total), COUNT(*) FROM orders o
        WHERE EXISTS (SELECT 1 FROM order_items i
                      WHERE i.order_id = o.order_id AND i.product_type = 'beverage')
    """
    assert _rows(package, query) == _reference(package, reference)


def test_two_distinct_counts_grouped_across_the_hop(package: Path) -> None:
    query = {**GROUPED_COUNT, "select": [*GROUPED_COUNT["select"], _measure("buyer_count")]}
    reference = """
        SELECT i.product_type, COUNT(DISTINCT o.order_id), COUNT(DISTINCT o.customer_id)
        FROM order_items i JOIN orders o ON o.order_id = i.order_id GROUP BY 1
    """
    assert _rows(package, query) == _reference(package, reference)


def test_a_lookup_after_the_hop_and_a_second_hop_down(package: Path) -> None:
    """orders -> items -> products, and customers -> orders -> items."""
    orders = {"select": [_measure("order_count")], "group_by": [CATEGORY]}
    reference = """
        SELECT p.category, COUNT(DISTINCT o.order_id) FROM orders o
        JOIN order_items i ON i.order_id = o.order_id JOIN products p ON p.sku = i.sku GROUP BY 1
    """
    assert _rows(package, orders) == _reference(package, reference)

    hot = {
        "select": [_measure("revenue")],
        "where": [{"field": CATEGORY, "op": "=", "value": "hot"}],
    }
    reference = """
        SELECT SUM(o.total) FROM orders o WHERE EXISTS (
          SELECT 1 FROM order_items i JOIN products p ON p.sku = i.sku
          WHERE i.order_id = o.order_id AND p.category = 'hot')
    """
    assert _rows(package, hot) == _reference(package, reference)

    credit = {
        "select": [_measure("credit")],
        "where": [{"field": TYPE, "op": "=", "value": "jaffle"}],
    }
    reference = """
        SELECT SUM(c.credit) FROM customers c WHERE EXISTS (
          SELECT 1 FROM orders o JOIN order_items i ON i.order_id = o.order_id
          WHERE o.customer_id = c.customer_id AND i.product_type = 'jaffle')
    """
    # Customer 10 once, though two of its orders hold a jaffle and one holds two beverages.
    assert _rows(package, credit) == _reference(package, reference) == [(300.0,)]

    buyers = {"select": [_measure("customer_count")], "group_by": [TYPE]}
    reference = """
        SELECT i.product_type, COUNT(DISTINCT o.customer_id)
        FROM order_items i JOIN orders o ON o.order_id = i.order_id GROUP BY 1
    """
    assert _rows(package, buyers) == _reference(package, reference)


def _where(op: str, value: Any, field: str = TYPE) -> dict[str, Any]:
    return {"select": [_measure("revenue")], "where": [{"field": field, "op": op, "value": value}]}


NEGATED = "'has a row that is not X' and 'has no row that is X' differ"


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        # Order revenue by product type: split over the items, or each order's full total?
        ({"select": [_measure("revenue")], "group_by": [TYPE]}, "is ambiguous across"),
        ({"select": [_measure("revenue", "avg")], "group_by": [TYPE]}, "is ambiguous across"),
        # "Orders without a beverage" and "orders with a non-beverage item" differ.
        (_where("!=", "beverage"), NEGATED),
        (_where("<>", "beverage"), NEGATED),
        (_where("NOT IN", ["beverage"]), NEGATED),
        (_where("NOT LIKE", "bev%"), NEGATED),
        (_where("IS DISTINCT FROM", "beverage"), NEGATED),
        (_where("IS NOT", "beverage"), NEGATED),
        (_where("IS NULL", None), NEGATED),
        (_where("=", None), NEGATED),
        (_where("=", False, HOT), NEGATED),
        (_where("=", "false", HOT), NEGATED),
        (_where("in", [False], HOT), NEGATED),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "aggregate",
                            "measure": "measure.hop.revenue",
                            "aggregation": "sum",
                            "filter": {"all": [{"field": TYPE, "op": "!=", "value": "beverage"}]},
                        },
                        "as": "revenue",
                    }
                ]
            },
            NEGATED,
        ),
        # orders -> customer -> sessions: many-to-many through the customer.
        ({"select": [_measure("order_count")], "group_by": [CHANNEL]}, "many-to-many"),
        # Coupons join orders on a code, not their key: two coupons could share it.
        ({"select": [_measure("face_value")], "where": [BEVERAGE]}, "join off the declared key"),
        # A pre-aggregated value has no one row per key to count.
        ({"select": [_measure("score", "avg")], "where": [BEVERAGE]}, "not defined over one row"),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "cumulative",
                            "input": {"measure": "measure.hop.revenue"},
                        },
                        "as": "running",
                    }
                ],
                "where": [BEVERAGE],
                "time": {"temporal_role": ROLE, "grain": "month"},
            },
            "not supported across a one-to-many hop",
        ),
    ],
    ids=[
        "sum",
        "avg",
        "not_equal",
        "angle_not_equal",
        "not_in",
        "not_like",
        "is_distinct_from",
        "is_not_value",
        "is_null",
        "equals_null",
        "boolean_false",
        "boolean_false_text",
        "boolean_in_false",
        "measure_filter",
        "many_to_many",
        "off_key_join",
        "non_additive",
        "cumulative",
    ],
)
def test_ambiguous_shapes_stay_refused(package: Path, query: dict[str, Any], reason: str) -> None:
    error = _refusal(package, query)
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert reason in error["why_invalid"]
    assert error["recovery_hints"]


def test_positive_null_and_boolean_tests_mean_exists(package: Path) -> None:
    for op, value, field, predicate in [
        ("IS NOT NULL", None, TYPE, "i.product_type IS NOT NULL"),
        ("=", True, HOT, "i.is_hot"),
    ]:
        reference = f"""
            SELECT SUM(o.total) FROM orders o WHERE EXISTS (
              SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND {predicate})
        """
        assert _rows(package, _where(op, value, field)) == _reference(package, reference)


def test_a_refusal_names_the_path_at_fault(package: Path) -> None:
    """The grouped sum is at fault, not the filter beside it, so the allocation hint shows."""
    query = {
        "select": [_measure("revenue")],
        "group_by": [TYPE],
        "where": [{"field": CATEGORY, "op": "=", "value": "hot"}],
    }
    error = _refusal(package, query)
    assert error["details"]["purpose"] == "group_by"
    assert error["details"]["target_entity"] == "entity.hop_item"
    kinds = [hint["kind"] for hint in error["recovery_hints"]]
    assert "requires_allocation_policy" in kinds


def test_a_refusal_never_offers_a_different_measure(package: Path) -> None:
    """A recovery used to hand back item revenue in place of order revenue, ready to run."""
    error = _refusal(package, {"select": [_measure("revenue")], "group_by": [TYPE]})
    assert "measure.hop.item_revenue" in error["details"]["compatible_measures"]  # named only
    queries = [error.get("closest_valid_query") or {}]
    queries += [hint.get("closest_valid_query") or {} for hint in error["recovery_hints"]]
    for query in queries:
        assert all(
            item["expression"]["measure"] == "measure.hop.revenue"
            for item in query.get("select", [])
        ), query


def test_rollup_safe_package_discloses_the_de_duplication(runtime_factory) -> None:
    """A grouped distinct count plus a filter across the hop used to take entity_in_terms_of;
    the filter moves it to the de-duplicated leaf, which reports its own step."""
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "group_by": ["dimension.jaffle_item_product_type"],
            "where": [
                {"field": "dimension.jaffle_item_product_name", "op": "=", "value": "tangaroo"}
            ],
        }
        result = runtime.query(query)
        steps = [step["kind"] for step in runtime.validate(query)["logical_plan"]["rewrite_steps"]]
        db_path = runtime.db_path
    finally:
        runtime.close()
    assert steps == ["fanout_dedup"]
    assert result["provenance_summary"]["rewrite_status"] == "rewritten"
    with duckdb.connect(db_path, read_only=True) as conn:
        expected = conn.execute(
            "SELECT product_type, COUNT(DISTINCT order_id) FROM jaffle_item"
            " WHERE product_name = 'tangaroo' GROUP BY 1"
        ).fetchall()
    assert sorted(tuple(row.values()) for row in result["rows"]) == sorted(expected)


# The de-duplicated leaf on every locally testable warehouse: a CTE, SELECT DISTINCT and an
# aggregate over its columns, with each dialect's quoting.
DIALECT_SQL = {
    "filtered": """WITH leaf_1__leaf_1_entity_rows AS (
SELECT DISTINCT
  orders.order_id AS __entity_key_1,
  orders.total AS __entity_value
FROM orders
INNER JOIN order_items ON orders.order_id = order_items.order_id
WHERE
  order_items.product_type = 'beverage'
),
leaf_1 AS (
SELECT
  SUM(leaf_1__leaf_1_entity_rows.__entity_value) AS m1
FROM leaf_1__leaf_1_entity_rows
)
SELECT
  base.m1 AS revenue
FROM leaf_1 AS base""",
    "grouped": """WITH leaf_1__leaf_1_entity_rows AS (
SELECT DISTINCT
  orders.order_id AS __entity_key_1,
  order_items.product_type AS g1,
  orders.order_id AS __entity_value
FROM orders
INNER JOIN order_items ON orders.order_id = order_items.order_id
),
leaf_1 AS (
SELECT
  leaf_1__leaf_1_entity_rows.g1 AS g1,
  COUNT(DISTINCT leaf_1__leaf_1_entity_rows.__entity_value) AS m1
FROM leaf_1__leaf_1_entity_rows
GROUP BY
  leaf_1__leaf_1_entity_rows.g1
)
SELECT
  base.g1 AS "dimension.hop_item_product_type",
  base.m1 AS orders
FROM leaf_1 AS base""",
}


@pytest.mark.parametrize("warehouse", ["duckdb", "postgres", "clickhouse", "ducklake"])
@pytest.mark.parametrize("shape", ["filtered", "grouped"])
def test_each_dialect_renders_the_de_duplicated_leaf(
    package: Path, warehouse: str, shape: str
) -> None:
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    query = {"filtered": FILTERED_SUM, "grouped": GROUPED_COUNT}[shape]
    sql = compile_query(config, Registry(config), {"version": 1, **query})["sql"]
    assert sql == DIALECT_SQL[shape]
