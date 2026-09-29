"""Measures across one-to-many hops count each row of their own entity once.

An order-grain measure filtered or grouped by an item dimension (orders -> order_items) used to
be refused with MIXED_GRAIN_INVALID. The leaf now keeps one row per (order key, output grain)
before it aggregates: filtered, an order counts once at all (a semi-join); grouped, a distinct
count counts it once in every product type it contains ("orders that included it"). Every answer
is checked on DuckDB against reference SQL written independently of the engine.

The package mirrors the D2 benchmark's: its item -> order relationship is inferred, with no
``rollup_safe`` opt-in, so the entity_in_terms_of shortcut does not apply.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.runtime import Runtime

# Order 1 has two beverages (the double-count trap), order 2 a beverage and a jaffle, order 3
# two jaffles, order 4 a beverage after Q4 2016, order 5 no items, and order 6 a beverage and a
# NULL total. Customer 10 has sessions on two channels, 11 on one, and 12 none.
SEED = """
CREATE TABLE orders AS SELECT * FROM (VALUES
  (1, 10, TIMESTAMP '2016-10-05 10:00:00', 10.0),
  (2, 10, TIMESTAMP '2016-11-01 10:00:00', 20.0),
  (3, 11, TIMESTAMP '2016-12-01 10:00:00', 30.0),
  (4, 12, TIMESTAMP '2017-01-15 10:00:00', 40.0),
  (5, 12, TIMESTAMP '2016-12-20 10:00:00', 50.0),
  (6, 11, TIMESTAMP '2016-10-10 10:00:00', NULL)
) AS t(order_id, customer_id, ordered_at, total);
CREATE TABLE order_items AS SELECT * FROM (VALUES
  (101, 1, 'coffee', 'beverage', 3.0),
  (102, 1, 'tea', 'beverage', 4.0),
  (201, 2, 'coffee', 'beverage', 3.0),
  (202, 2, 'toast', 'jaffle', 17.0),
  (301, 3, 'toast', 'jaffle', 15.0),
  (302, 3, 'melt', 'jaffle', 15.0),
  (401, 4, 'tea', 'beverage', 40.0),
  (601, 6, 'coffee', 'beverage', 5.0)
) AS t(item_id, order_id, sku, product_type, revenue);
CREATE TABLE products AS SELECT * FROM (VALUES
  ('coffee', 'hot'), ('tea', 'hot'), ('toast', 'food'), ('melt', 'food')
) AS t(sku, category);
CREATE TABLE customers AS SELECT * FROM (VALUES
  (10, 100.0, 1.5), (11, 200.0, 2.5), (12, 400.0, 3.5)
) AS t(customer_id, credit, score);
CREATE TABLE sessions AS SELECT * FROM (VALUES
  (1, 10, 'web'), (2, 10, 'app'), (3, 11, 'web')
) AS t(session_id, customer_id, channel);
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
  entities: {order: {}, customer: {}}
  times:
    ordered_at: {label: Order time, column: ordered_at, kind: timestamp, class: event_time,
      default: true}
  measures:
    order_count: {label: Orders, kind: entity_count, entity_key: order_id,
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
}
ENTITIES = {
    "order": ["order_id", "orders"],
    "item": ["item_id", "order_items"],
    "product": ["sku", "products"],
    "customer": ["customer_id", "customers"],
    "session": ["session_id", "sessions"],
}

ROLE = "temporal_role.hop_order_ordered_at"
TYPE = "dimension.hop_item_product_type"
CATEGORY = "dimension.hop_product_category"
CHANNEL = "dimension.hop_session_channel"
Q4_2016 = {"temporal_role": ROLE, "grain": "quarter", "start": "2016-10-01", "end": "2017-01-01"}
IN_Q4 = "o.ordered_at >= TIMESTAMP '2016-10-01' AND o.ordered_at < TIMESTAMP '2017-01-01'"
BEVERAGE = {"field": TYPE, "op": "=", "value": "beverage"}


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("hop") / "hop"
    entities = {
        name: {"label": name, "key": [key], "model": model}
        for name, (key, model) in ENTITIES.items()
    }
    files = {
        "package.yml": PACKAGE,
        "data/seed.sql": SEED,
        "graph.yml": yaml.safe_dump({"graph": {"entities": entities}}),
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


def test_orders_and_item_revenue_by_product_type(package: Path) -> None:
    """d022: per item product type, the orders that included it and the item revenue."""
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
    """d032 and the double-count trap: order 1's two beverages add its total once."""
    query = {"select": [_measure("revenue")], "where": [BEVERAGE], "time": Q4_2016}
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


def test_a_lookup_after_the_hop_and_a_second_hop_down(package: Path) -> None:
    """orders -> items -> products, and customers -> orders -> items."""
    orders = {"select": [_measure("order_count")], "group_by": [CATEGORY]}
    reference = """
        SELECT p.category, COUNT(DISTINCT i.order_id)
        FROM order_items i JOIN products p ON p.sku = i.sku GROUP BY 1
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


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        # Order revenue by product type: split over the items, or each order's full total?
        ({"select": [_measure("revenue")], "group_by": [TYPE]}, "is ambiguous across"),
        ({"select": [_measure("revenue", "avg")], "group_by": [TYPE]}, "is ambiguous across"),
        # "Orders without a beverage" and "orders with a non-beverage item" differ.
        (
            {"select": [_measure("revenue")], "where": [{**BEVERAGE, "op": "!="}]},
            "has no row that is",
        ),
        (
            {
                "select": [_measure("revenue")],
                "where": [{**BEVERAGE, "op": "not in", "value": ["beverage"]}],
            },
            "has no row that is",
        ),
        # orders -> customer -> sessions: many-to-many through the customer.
        ({"select": [_measure("order_count")], "group_by": [CHANNEL]}, "many-to-many"),
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
    ids=["sum", "avg", "not_equal", "not_in", "many_to_many", "non_additive", "cumulative"],
)
def test_ambiguous_shapes_stay_refused(package: Path, query: dict[str, Any], reason: str) -> None:
    error = _refusal(package, query)
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert reason in error["why_invalid"]


def test_a_refusal_never_offers_a_different_measure(package: Path) -> None:
    """d032's recovery handed back item revenue in place of order revenue, ready to run."""
    error = _refusal(package, {"select": [_measure("revenue")], "group_by": [TYPE]})
    assert "measure.hop.item_revenue" in error["details"]["compatible_measures"]  # named only
    queries = [error.get("closest_valid_query") or {}]
    queries += [hint.get("closest_valid_query") or {} for hint in error["recovery_hints"]]
    for query in queries:
        assert all(
            item["expression"]["measure"] == "measure.hop.revenue"
            for item in query.get("select", [])
        ), query
