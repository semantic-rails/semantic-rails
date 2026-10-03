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
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import ColumnRefExpr
from semantic_rails.policies import row_filters_for_context
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PathPreferenceConfig, SemanticPolicyConfig
from tests.semantic_rails.result_helpers import typed_rows

# Order 1 has two beverages (the double-count trap), order 2 a beverage and a jaffle, order 3
# two jaffles, order 4 a beverage after Q4 2016, order 5 no items, and order 6 a beverage and a
# NULL total. Two orders with a NULL key and the same total hold beverages with a NULL order
# key: no join matches them, so they count nowhere. Customer 10 has sessions on two channels
# (two on web), 11 on one, and 12 none. Coupons join orders on their code, which is not their
# key, and orders 4 to 6 have none.
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
  (10, 100.0, 1.5, 'retail'), (11, 200.0, 2.5, 'wholesale'), (12, 400.0, 3.5, 'retail')
) AS t(customer_id, credit, score, segment);
CREATE TABLE sessions AS SELECT * FROM (VALUES
  (1, 10, 'web'), (2, 10, 'app'), (3, 11, 'web'), (4, 10, 'web')
) AS t(session_id, customer_id, channel);
CREATE TABLE coupons AS SELECT * FROM (VALUES (1, 'A', 5.0), (2, 'B', 7.0)) AS t(coupon_id, code, face_value);
CREATE TABLE payments AS SELECT * FROM (VALUES (1, 1, 5.0), (2, 1, 5.0)) AS t(payment_id, order_id, amount);
CREATE TABLE order_payments AS SELECT * FROM (VALUES (1, 1, 'card'), (2, 2, 'cash')) AS t(payment_id, order_id, method);
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
  entities: {item: {}, order: {}, product: {expr: sku}, receipt: {expr: order_id}}
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
  dimensions:
    segment: {label: Customer segment, kind: categorical}
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
    "payments": """
model:
  id: payments
  relation: payments
  grain: [payment_id]
  entities: {receipt: {}}
  measures:
    paid: {label: Paid, kind: aggregate, expr: amount, accumulation: {kind: flow},
      value_type: currency}
""",
    "order_payments": """
model:
  id: order_payments
  relation: order_payments
  entities: {payment: {}, order: {}}
  dimensions:
    method: {label: Payment method, kind: categorical}
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
    "payment": ["payment_id", "order_payments"],
    # Receipts are keyed by their order, but each payment is a row.
    "receipt": ["order_id", "payments"],
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
METHOD = "dimension.hop_payment_method"
COUPON = "dimension.hop_coupon_id"
SEGMENT = "dimension.hop_customer_segment"
Q4_2016 = {"temporal_role": ROLE, "grain": "quarter", "start": "2016-10-01", "end": "2017-01-01"}
IN_Q4 = "o.ordered_at >= TIMESTAMP '2016-10-01' AND o.ordered_at < TIMESTAMP '2017-01-01'"
BEVERAGE = {"field": TYPE, "op": "=", "value": "beverage"}
WEB = {"field": CHANNEL, "op": "=", "value": "web"}
HOT_ITEM = {"field": HOT, "op": "=", "value": True}
FILTERED_SUM = {
    "select": [{"expression": {"measure": "measure.hop.revenue"}, "as": "revenue"}],
    "where": [BEVERAGE],
}
GROUPED_COUNT = {
    "select": [{"expression": {"measure": "measure.hop.order_count"}, "as": "orders"}],
    "group_by": [TYPE],
}
ORDER = "entity.hop_order"


def _compare(column: str, op: str, value: Any, entity: str = ORDER) -> dict[str, Any]:
    left = {"kind": "column", "entity": entity, "column": column}
    return {
        "kind": "comparison",
        "op": op,
        "left": left,
        "right": {"kind": "literal", "value": value},
    }


def _if(
    aggregation: str, condition: dict[str, Any], value: str = "", entity: str = ORDER
) -> dict[str, Any]:
    """An aggregate_if over the rows of ``entity``; without ``value`` it counts them."""
    expression = {"kind": "aggregate_if", "aggregation": aggregation, "condition": condition}
    if value:
        expression["value"] = {"kind": "column", "entity": entity, "column": value}
    return expression


def _ratio(numerator: dict[str, Any], denominator: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "ratio", "numerator": numerator, "denominator": denominator}


# Customer 10's orders, and their revenue: the conditional forms of order_count and revenue.
OWN_ORDERS_IF = _if("count_distinct", _compare("customer_id", "=", 10), "order_id")
OWN_REVENUE_IF = _if("sum", _compare("customer_id", "=", 10), "total")
OWN_SHARE = _ratio(OWN_ORDERS_IF, {"measure": "measure.hop.order_count"})


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
    return _normal(tuple(row.values()) for row in typed_rows(result))


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


@pytest.mark.parametrize(
    ("measure", "field", "value", "reference", "expected"),
    [
        (
            "order_count",
            TYPE,
            "beverage",
            "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i "
            "WHERE i.order_id = o.order_id AND i.product_type = 'beverage')",
            4,
        ),
        (
            "revenue",
            TYPE,
            "beverage",
            "SELECT SUM(o.total) FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i "
            "WHERE i.order_id = o.order_id AND i.product_type = 'beverage')",
            70,
        ),
        (
            "order_count",
            CHANNEL,
            "web",
            "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM sessions s "
            "WHERE s.customer_id = o.customer_id AND s.channel = 'web')",
            4,
        ),
        (
            "revenue",
            CHANNEL,
            "web",
            "SELECT SUM(o.total) FROM orders o WHERE EXISTS (SELECT 1 FROM sessions s "
            "WHERE s.customer_id = o.customer_id AND s.channel = 'web')",
            60,
        ),
        (
            "face_value",
            TYPE,
            "beverage",
            "SELECT SUM(c.face_value) FROM coupons c WHERE EXISTS (SELECT 1 FROM orders o "
            "JOIN order_items i ON i.order_id = o.order_id "
            "WHERE o.coupon_code = c.code AND i.product_type = 'beverage')",
            5,
        ),
    ],
    ids=[
        "parent_count",
        "parent_sum",
        "lookup_then_child_count",
        "lookup_then_child_sum",
        "alternate_key",
    ],
)
def test_child_filters_use_exists_without_multiplying_parent_rows(
    package: Path, measure: str, field: str, value: str, reference: str, expected: int
) -> None:
    query = {"select": [_measure(measure)], "where": [{"field": field, "op": "=", "value": value}]}
    result = _run(package, query)
    assert (
        _normal(tuple(row.values()) for row in typed_rows(result))
        == _reference(package, reference)
        == [(expected,)]
    )
    assert "EXISTS (" in result["rendered_sql"]
    assert "SELECT DISTINCT" not in result["rendered_sql"]
    assert "JOIN" not in result["rendered_sql"]


def test_measure_bound_child_filter_does_not_change_unfiltered_sibling(package: Path) -> None:
    filtered = {
        "kind": "aggregate",
        "measure": "measure.hop.revenue",
        "aggregation": "sum",
        "filter": {"all": [{"field": CHANNEL, "op": "=", "value": "app"}]},
    }
    query = {
        "select": [{"expression": filtered, "as": "matching"}, _measure("revenue", alias="all")]
    }
    reference = """
        SELECT (SELECT SUM(o.total) FROM orders o WHERE EXISTS (
          SELECT 1 FROM sessions s WHERE s.customer_id = o.customer_id AND s.channel = 'app')),
          (SELECT SUM(total) FROM orders)
    """
    assert _rows(package, query) == _reference(package, reference) == [(30.0, 290.0)]


@pytest.mark.parametrize("bound_filter", [False, True], ids=["query", "measure"])
@pytest.mark.parametrize("fill", [False, True], ids=["bounded", "filled"])
def test_time_bounded_child_filter_keeps_window_observation(
    package: Path, bound_filter: bool, fill: bool
) -> None:
    expression: dict[str, Any] = {"measure": "measure.hop.revenue"}
    if bound_filter:
        expression.update(kind="aggregate", aggregation="sum", filter={"all": [BEVERAGE]})
    query = {
        "select": [{"expression": expression, "as": "revenue"}],
        "where": [] if bound_filter else [BEVERAGE],
        "time": {
            "temporal_role": ROLE,
            "grain": "month",
            "start": "2016-10-01",
            "end": "2017-04-01",
            "fill": fill,
        },
    }
    matching = """
        SELECT DATE_TRUNC('month', o.ordered_at) AS month, SUM(o.total) AS revenue
        FROM orders o
        WHERE o.ordered_at >= TIMESTAMP '2016-10-01'
          AND o.ordered_at < TIMESTAMP '2017-04-01'
          AND EXISTS (SELECT 1 FROM order_items i
                      WHERE i.order_id = o.order_id AND i.product_type = 'beverage')
        GROUP BY month
    """
    reference = matching
    if fill:
        reference = f"""
            WITH matching AS ({matching})
            SELECT months.month, COALESCE(matching.revenue, 0)
            FROM GENERATE_SERIES(TIMESTAMP '2016-10-01', TIMESTAMP '2017-03-01',
                                 INTERVAL '1 month') AS months(month)
            LEFT JOIN matching USING (month)
        """
    result = _run(package, query)
    assert _normal(tuple(row.values()) for row in typed_rows(result)) == _reference(
        package, reference
    )
    sql = result["rendered_sql"]
    assert "EXISTS (" in sql and "SELECT DISTINCT" not in sql
    # Rewritten fanout leaves retain observation inside the window, including filled buckets.
    assert "coverage_" not in sql and sql.count("FROM orders") == 1


@pytest.mark.parametrize("match", ["any", "none"])
@pytest.mark.parametrize("fill", [False, True], ids=["bounded", "filled"])
def test_time_bounded_child_groups_keep_window_observation(
    package: Path, match: str, fill: bool
) -> None:
    """A child group lowers in the same semi-join leaf as a flat child filter."""
    query = {
        "select": [{"expression": {"measure": "measure.hop.revenue"}, "as": "revenue"}],
        "where": [{"child": "entity.hop_item", "match": match, "where": [BEVERAGE, HOT_ITEM]}],
        "time": {
            "temporal_role": ROLE,
            "grain": "month",
            "start": "2016-10-01",
            "end": "2017-04-01",
            "fill": fill,
        },
    }
    negation = "NOT " if match == "none" else ""
    matching = f"""
        SELECT DATE_TRUNC('month', o.ordered_at) AS month, SUM(o.total) AS revenue
        FROM orders o
        WHERE o.ordered_at >= TIMESTAMP '2016-10-01'
          AND o.ordered_at < TIMESTAMP '2017-04-01'
          AND {negation}EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id
                                AND i.product_type = 'beverage' AND i.is_hot)
        GROUP BY month
    """
    reference = matching
    if fill:
        reference = f"""
            WITH matching AS ({matching})
            SELECT months.month, COALESCE(matching.revenue, 0)
            FROM GENERATE_SERIES(TIMESTAMP '2016-10-01', TIMESTAMP '2017-03-01',
                                 INTERVAL '1 month') AS months(month)
            LEFT JOIN matching USING (month)
        """
    result = _run(package, query)
    assert _normal(tuple(row.values()) for row in typed_rows(result)) == _reference(
        package, reference
    )
    sql = result["rendered_sql"]
    assert f"{negation}EXISTS (" in sql and "SELECT DISTINCT" not in sql
    assert "coverage_" not in sql and sql.count("FROM orders") == 1


def test_child_filter_correlates_every_authored_join_column(package: Path) -> None:
    config = load_package_config(str(package))
    config = replace(
        config,
        relationships=[
            replace(
                rel, source_columns=["order_id", "revenue"], target_columns=["order_id", "total"]
            )
            if rel.id == "relationship.order_items_order"
            else rel
            for rel in config.relationships
        ],
    )
    compiled = compile_query(config, Registry(config), {"version": 1, **FILTERED_SUM})
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        actual = _normal(conn.execute(compiled["prepared_query"].sql).fetchall())
    reference = """
        SELECT SUM(o.total) FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i
          WHERE i.order_id = o.order_id AND i.revenue = o.total AND i.product_type = 'beverage')
    """
    assert actual == _reference(package, reference) == [(40.0,)]


def test_a_child_value_authored_at_parent_grain_still_refuses(package: Path) -> None:
    config = load_package_config(str(package))
    config = replace(
        config,
        measures=[
            replace(measure, expr=ColumnRefExpr(entity="entity.hop_item", column="revenue"))
            if measure.id == "measure.hop.revenue"
            else measure
            for measure in config.measures
        ],
    )
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), {"version": 1, **FILTERED_SUM})
    assert caught.value.code == "MIXED_GRAIN_INVALID"
    assert caught.value.details["purpose"] == "measure_expr"


def test_measure_filter_cannot_bypass_the_single_crossing_condition_guard(package: Path) -> None:
    query = _bound_filter({"field": CHANNEL, "op": "=", "value": "web"})
    error = _refusal(package, {**query, "where": [BEVERAGE]})
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert TWO_CONDITIONS in error["why_invalid"]


@pytest.mark.parametrize("cardinality", ["N:N", "unknown"])
def test_child_filter_does_not_admit_many_to_many_or_unknown_hops(
    package: Path, cardinality: str
) -> None:
    config = load_package_config(str(package))
    config = replace(
        config,
        relationships=[
            replace(rel, cardinality=cardinality, safety="requires_rewrite")
            if rel.id == "relationship.order_items_order"
            else rel
            for rel in config.relationships
        ],
    )
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), {"version": 1, **FILTERED_SUM})
    assert caught.value.code == "MIXED_GRAIN_INVALID"


@pytest.mark.parametrize(
    ("expression", "clause", "expected"),
    [
        ({"measure": "measure.hop.order_count"}, BEVERAGE, 4),
        ({"measure": "measure.hop.revenue"}, BEVERAGE, 70),
        (OWN_REVENUE_IF, BEVERAGE, 30),
        (OWN_SHARE, BEVERAGE, 0.5),
    ],
    ids=["parent_count", "parent_sum", "conditional_sum", "conditional_ratio"],
)
def test_clickhouse_uses_equivalent_parent_deduplication(
    package: Path, expression: dict[str, Any], clause: dict[str, Any], expected: float
) -> None:
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse="clickhouse"))
    query = {"version": 1, "select": [{"expression": expression, "as": "v"}], "where": [clause]}
    compiled = compile_query(config, Registry(config), query)
    sql = compiled["prepared_query"].sql.removesuffix("\nSETTINGS join_use_nulls = 1")
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        actual = _normal(conn.execute(sql).fetchall())
    assert actual == _rows(package, query) == [(expected,)]
    assert "SELECT DISTINCT" in sql
    assert "EXISTS" not in sql


@pytest.mark.parametrize(
    ("select", "clause"),
    [
        (_measure("face_value"), BEVERAGE),
        (_measure("order_count"), WEB),
        (_measure("revenue"), WEB),
        ({"expression": OWN_REVENUE_IF, "as": "v"}, WEB),
        ({"expression": OWN_SHARE, "as": "v"}, WEB),
    ],
    ids=[
        "alternate_key",
        "lookup_then_child_count",
        "lookup_then_child_sum",
        "lookup_then_child_conditional_sum",
        "lookup_then_child_conditional_ratio",
    ],
)
def test_clickhouse_refuses_child_filter_paths_requiring_exists(
    package: Path, select: dict[str, Any], clause: dict[str, Any]
) -> None:
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse="clickhouse"))
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(
            config, Registry(config), {"version": 1, "select": [select], "where": [clause]}
        )
    assert caught.value.code == "MIXED_GRAIN_INVALID"
    assert "ClickHouse" in caught.value.details["why_invalid"]


def test_a_conditional_aggregate_of_rows_of_unknown_grain(package: Path) -> None:
    """Without the paid measure nothing says receipts hold a row per payment. EXISTS reads each
    payment once anyway; ClickHouse's one row per receipt would merge order 1's two payments of
    5, so it refuses all but the aggregations that merging cannot change."""
    config = load_package_config(str(package))
    config = replace(config, measures=[m for m in config.measures if m.id != "measure.hop.paid"])
    receipt = "entity.hop_receipt"
    payments = _if("count_distinct", _compare("amount", ">", 0, receipt), "payment_id", receipt)
    for expression, value in ((PAID_IF, 10), (payments, 2)):
        query = {
            "version": 1,
            "select": [{"expression": expression, "as": "v"}],
            "where": [BEVERAGE],
        }
        compiled = compile_query(config, Registry(config), query)
        with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
            assert conn.execute(compiled["prepared_query"].sql).fetchall() == [(value,)]
    reference = """
        SELECT SUM(p.amount), COUNT(DISTINCT p.payment_id) FROM payments p WHERE EXISTS (
          SELECT 1 FROM order_items i WHERE i.order_id = p.order_id AND i.product_type = 'beverage')
    """
    assert _reference(package, reference) == [(10.0, 2)]
    clickhouse = replace(config, package=replace(config.package, warehouse="clickhouse"))
    query["select"] = [{"expression": payments, "as": "v"}]
    sql = compile_query(clickhouse, Registry(clickhouse), query)["sql"]
    assert "SELECT DISTINCT" in sql
    query["select"] = [{"expression": PAID_IF, "as": "v"}]
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(clickhouse, Registry(clickhouse), query)
    assert caught.value.code == "MIXED_GRAIN_INVALID"
    assert "could merge rows that share a key" in caught.value.details["why_invalid"]


@pytest.mark.parametrize(
    "lookup",
    [
        {"group_by": [COUPON]},
        {"select": [_measure("order_count"), {"expression": {"dimension": COUPON}}]},
        {"where": [{"field": COUPON, "op": "=", "value": 1}]},
        {"where": [{"field": COUPON, "op": "IS NULL", "value": None}]},
    ],
    ids=["grouped_lookup", "selected_lookup", "lookup_filter", "null_lookup_filter"],
)
@pytest.mark.parametrize("bound_filter", [False, True], ids=["where", "measure_filter"])
@pytest.mark.parametrize("clause", [BEVERAGE, WEB], ids=["descent", "lookup_then_child"])
def test_clickhouse_keeps_supported_lookups_beside_a_child_filter(
    package: Path, lookup: dict[str, Any], bound_filter: bool, clause: dict[str, Any]
) -> None:
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse="clickhouse"))
    query = {"version": 1, "select": [_measure("order_count")], **lookup}
    if bound_filter:
        expression = {
            "kind": "aggregate",
            "measure": "measure.hop.order_count",
            "aggregation": "count_distinct",
            "filter": {"all": [clause]},
        }
        query["select"] = [{"expression": expression, "as": "orders"}, *query["select"][1:]]
    else:
        query["where"] = [clause, *query.get("where", [])]
    if clause == WEB:
        with pytest.raises(SemanticLayerError) as caught:
            compile_query(config, Registry(config), query)
        assert caught.value.code == "MIXED_GRAIN_INVALID"
        assert "ClickHouse" in caught.value.details["why_invalid"]
        return
    sql = compile_query(config, Registry(config), query)["prepared_query"].sql.removesuffix(
        "\nSETTINGS join_use_nulls = 1"
    )
    assert "SELECT DISTINCT" in sql
    assert "INNER JOIN coupons" in sql
    assert "EXISTS" not in sql
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        rows = _normal(conn.execute(sql).fetchall())
    if "group_by" in lookup or "select" in lookup:
        reference = """
            SELECT c.coupon_id, COUNT(DISTINCT o.order_id) FROM orders o
            JOIN coupons c ON c.code = o.coupon_code
            WHERE EXISTS (SELECT 1 FROM order_items i
              WHERE i.order_id = o.order_id AND i.product_type = 'beverage') GROUP BY 1
        """
    else:
        condition = (
            "c.coupon_id IS NULL" if lookup["where"][0]["value"] is None else "c.coupon_id = 1"
        )
        reference = f"""
            SELECT CASE WHEN COUNT(*) > 0 THEN COUNT(DISTINCT o.order_id) ELSE NULL END FROM orders o
            JOIN coupons c ON c.code = o.coupon_code WHERE {condition}
            AND EXISTS (SELECT 1 FROM order_items i
              WHERE i.order_id = o.order_id AND i.product_type = 'beverage')
        """
    assert rows == _reference(package, reference)


@pytest.fixture
def diamond_package(tmp_path: Path) -> Path:
    root = tmp_path / "diamond"
    edges = {
        "account_client": ("account", "client", "client_id"),
        "district_client": ("district", "client", "client_id"),
        "account_branch": ("account", "branch", "branch_id"),
        "zone_branch": ("zone", "branch", "branch_id"),
        "district_zone": ("district", "zone", "zone_id"),
    }
    graph = {
        "entities": {
            entity: {
                "label": entity,
                "key": [f"{entity}_id"],
                "model": "branches" if entity == "branch" else f"{entity}s",
            }
            for entity in ("account", "client", "branch", "zone", "district")
        },
        "relationships": {
            name: {
                "id": f"relationship.{name}",
                "entities": [source, target],
                "cardinality": "many_to_one",
                "via": [column],
                "target": [column],
            }
            for name, (source, target, column) in edges.items()
        },
    }
    seed = """
        CREATE TABLE accounts AS SELECT * FROM (VALUES (1, 10, 100, 50), (2, 20, 200, 70))
          AS t(account_id, client_id, branch_id, amount);
        CREATE TABLE clients AS SELECT * FROM (VALUES (10), (20)) AS t(client_id);
        CREATE TABLE branches AS SELECT * FROM (VALUES (100, 'east'), (200, 'west'))
          AS t(branch_id, name);
        CREATE TABLE zones AS SELECT * FROM (VALUES (1000, 100), (2000, 200))
          AS t(zone_id, branch_id);
        CREATE TABLE districts AS SELECT * FROM (VALUES
          (1, 10, 2000, 'premium'), (2, 20, 1000, 'standard'))
          AS t(district_id, client_id, zone_id, category);
    """
    models: dict[str, Any] = {
        "accounts": {
            "measures": {
                "amount": {
                    "label": "Amount",
                    "kind": "aggregate",
                    "expr": "amount",
                    "accumulation": {"kind": "flow"},
                    "value_type": "currency",
                }
            }
        },
        "clients": {},
        "branches": {"dimensions": {"name": {"label": "Name", "kind": "categorical"}}},
        "zones": {},
        "districts": {"dimensions": {"category": {"label": "Category", "kind": "categorical"}}},
    }
    files = {
        "package.yml": PACKAGE.replace("hop", "diamond"),
        "graph.yml": yaml.safe_dump({"graph": graph}),
        "data/seed.sql": seed,
        **{
            f"models/{name}.yml": yaml.safe_dump(
                {
                    "model": {
                        "id": name,
                        "relation": name,
                        "entities": {"branch" if name == "branches" else name[:-1]: {}},
                        **parts,
                    }
                }
            )
            for name, parts in models.items()
        },
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return root


@pytest.mark.parametrize("alternate_key", [False, True], ids=["lookup_first", "alternate_key"])
@pytest.mark.parametrize("bound_filter", [False, True], ids=["where", "measure_filter"])
@pytest.mark.parametrize("beside_lookup", [False, True], ids=["ungrouped", "lookup_group"])
def test_new_child_filter_paths_require_one_candidate_or_a_pin(
    diamond_package: Path, alternate_key: bool, bound_filter: bool, beside_lookup: bool
) -> None:
    clause = {"field": "dimension.diamond_district_category", "op": "=", "value": "premium"}
    expression: dict[str, Any] = {"measure": "measure.diamond.amount"}
    query: dict[str, Any] = {"version": 1, "select": [{"expression": expression, "as": "amount"}]}
    if bound_filter:
        expression.update(kind="aggregate", aggregation="sum", filter={"all": [clause]})
    else:
        query["where"] = [clause]
    if beside_lookup:
        query["group_by"] = ["dimension.diamond_branch_name"]
    config = load_package_config(str(diamond_package))
    if alternate_key:
        config = replace(
            config,
            relationships=[
                replace(rel, cardinality="1:N") if rel.id == "relationship.account_client" else rel
                for rel in config.relationships
            ],
        )
    # Two routes and neither is the account's own key: refused until the package records one.
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), query)
    assert caught.value.code == "AMBIGUOUS_PATH"
    assert caught.value.details["reason"] == "route_decision_required"
    assert caught.value.details["target"] == "entity.diamond_district"
    routes = [
        (["relationship.account_client", "relationship.district_client"], 50, "east"),
        (
            [
                "relationship.account_branch",
                "relationship.zone_branch",
                "relationship.district_zone",
            ],
            70,
            "west",
        ),
    ]
    options = caught.value.details["clarification"]["options"]
    assert [option["relationship_path"] for option in options] == [path for path, _, _ in routes]
    # Seed once; pinning must select the authored route even when it is longer.
    _run(diamond_package, {"select": [{"expression": {"measure": "measure.diamond.amount"}}]})
    for path, expected, branch in routes:
        pinned = replace(
            config,
            path_preferences=[
                PathPreferenceConfig(
                    source_entity="entity.diamond_account",
                    target_entity="entity.diamond_district",
                    relationship_path=path,
                )
            ],
        )
        compiled = compile_query(pinned, Registry(pinned), query)
        selection = next(
            row
            for row in compiled["logical_plan"].measure_plans[0].path_selections
            if row.target_entity == "entity.diamond_district"
        )
        assert selection.candidate_paths == [path]
        with duckdb.connect(str(diamond_package / "data/warehouse.duckdb"), read_only=True) as conn:
            actual = conn.execute(compiled["prepared_query"].sql).fetchall()
        reference = (
            "SELECT SUM(a.amount) FROM accounts a WHERE EXISTS (SELECT 1 FROM districts d "
            "WHERE d.client_id = a.client_id AND d.category = 'premium')"
            if expected == 50
            else "SELECT SUM(a.amount) FROM accounts a WHERE EXISTS (SELECT 1 FROM zones z "
            "JOIN districts d ON d.zone_id = z.zone_id "
            "WHERE z.branch_id = a.branch_id AND d.category = 'premium')"
        )
        assert _reference(diamond_package, reference) == [(expected,)]
        assert actual == ([(branch, expected)] if beside_lookup else [(expected,)])


@pytest.mark.parametrize("form", ["conditional", "ratio"])
def test_a_conditional_aggregate_takes_the_same_child_route_rule(
    diamond_package: Path, form: str
) -> None:
    """An aggregate_if, alone or as a ratio operand, refuses two routes and takes a pin."""
    account = "entity.diamond_account"
    large = _if("sum", _compare("amount", ">", 60, account), "amount", account)
    expression = (
        large if form == "conditional" else _ratio(large, {"measure": "measure.diamond.amount"})
    )
    query = {
        "version": 1,
        "select": [{"expression": expression, "as": "value"}],
        "where": [{"field": "dimension.diamond_district_category", "op": "=", "value": "premium"}],
    }
    config = load_package_config(str(diamond_package))
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), query)
    assert caught.value.code == "AMBIGUOUS_PATH"
    assert caught.value.details["reason"] == "route_decision_required"
    _run(diamond_package, {"select": [{"expression": {"measure": "measure.diamond.amount"}}]})
    # The client route reaches account 1 (50, so no amount over 60), the branch route account 2
    # (70). A sum of nothing reads NULL, and so does a ratio over it.
    routes = [
        (["relationship.account_client", "relationship.district_client"], None),
        (
            [
                "relationship.account_branch",
                "relationship.zone_branch",
                "relationship.district_zone",
            ],
            70 if form == "conditional" else 1,
        ),
    ]
    for path, expected in routes:
        pinned = replace(
            config,
            path_preferences=[PathPreferenceConfig(account, "entity.diamond_district", path)],
        )
        compiled = compile_query(pinned, Registry(pinned), query)
        assert "EXISTS (" in compiled["sql"] and "JOIN districts" not in compiled["sql"]
        with duckdb.connect(str(diamond_package / "data/warehouse.duckdb"), read_only=True) as conn:
            assert conn.execute(compiled["prepared_query"].sql).fetchall() == [(expected,)]


HOT_ITEMS = {"dimension": HOT, "attribute": "hot", "type": "boolean"}
OWN_ORDERS = {"dimension": "dimension.hop_order_customer_id", "attribute": "customer",
              "type": "integer"}  # fmt: skip


def _under_row_policy(
    package: Path, policy: dict[str, Any], attributes: dict[str, Any], query: dict[str, Any]
) -> dict[str, Any]:
    config = load_package_config(str(package))
    config = replace(
        config,
        semantic_policies=[SemanticPolicyConfig(id="policy.hop.rows", kind="row_filter",
                                                config=policy)],
    )  # fmt: skip
    context = RequestContext(attributes=attributes).to_policy_context()
    return compile_query(
        config,
        Registry(config),
        {"version": 1, **query},
        row_filters=row_filters_for_context(config, context),
    )


@pytest.mark.parametrize(
    ("policy", "attributes"),
    [(HOT_ITEMS, {"hot": True}), (OWN_ORDERS, {"customer": 10})],
    ids=["child_policy", "parent_policy"],
)
@pytest.mark.parametrize(
    "expression",
    [
        {"measure": "measure.hop.order_count"},
        {"measure": "measure.hop.revenue"},
        OWN_REVENUE_IF,
        OWN_SHARE,
    ],
    ids=["order_count", "revenue", "conditional_sum", "conditional_ratio"],
)
def test_child_filters_stay_denied_under_a_row_policy(
    package: Path, policy: dict[str, Any], attributes: dict[str, Any], expression: dict[str, Any]
) -> None:
    """A row policy qualifies only a query that reads its one relation; EXISTS reads two."""
    query = {"select": [{"expression": expression, "as": "v"}], "where": [BEVERAGE]}
    with pytest.raises(SemanticLayerError) as caught:
        _under_row_policy(package, policy, attributes, query)
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details["reason"] == "row_filter_unsupported_query"


def test_a_parent_row_policy_still_answers_without_a_child_filter(package: Path) -> None:
    query = {"select": [_measure("revenue")]}
    prepared = _under_row_policy(package, OWN_ORDERS, {"customer": 10}, query)["prepared_query"]
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        actual = _normal(conn.execute(prepared.sql, [10]).fetchall())
    reference = "SELECT SUM(total) FROM orders WHERE customer_id = 10"
    assert actual == _reference(package, reference) == [(30.0,)]


_HAS_BEVERAGE = (
    "SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND i.product_type = 'beverage'"
)
_HAS_WEB_SESSION = (
    "SELECT 1 FROM sessions s WHERE s.customer_id = o.customer_id AND s.channel = 'web'"
)


@pytest.mark.parametrize(
    ("measure", "clause", "child", "expected"),
    [
        ("order_count", BEVERAGE, _HAS_BEVERAGE, [(1, 2), (None, 2)]),
        ("order_count", WEB, _HAS_WEB_SESSION, [(1, 2), (2, 1), (None, 1)]),
        ("revenue", BEVERAGE, _HAS_BEVERAGE, [(1, 30.0), (None, 40.0)]),
    ],
    ids=["count_by_child", "count_by_lookup_then_child", "sum_by_child"],
)
def test_a_lookup_beside_a_child_filter_keeps_parents_it_finds_no_match_for(
    package: Path, measure: str, clause: dict[str, Any], child: str, expected: list[tuple[Any, ...]]
) -> None:
    """Grouped by coupon, orders without one stay in a NULL group, as in the ordinary leaf."""
    rows = _rows(package, {"select": [_measure(measure)], "group_by": [COUPON], "where": [clause]})
    value = "COUNT(*)" if measure == "order_count" else "SUM(o.total)"
    reference = f"""
        SELECT c.coupon_id, {value} FROM orders o LEFT JOIN coupons c ON c.code = o.coupon_code
        WHERE EXISTS ({child}) GROUP BY 1
    """
    assert rows == _reference(package, reference) == _normal(expected)
    ungrouped = _rows(package, {"select": [_measure(measure)], "where": [clause]})
    assert ungrouped == _normal([(sum(row[-1] for row in rows),)])


def test_a_lookup_beside_a_child_grouping_keeps_parents_it_finds_no_match_for(
    package: Path,
) -> None:
    """Grouped by type and coupon, the de-duplicated leaf keeps orders 4 and 6 (a beverage
    each, no coupon) under a NULL coupon, so its groups add up to the count by type alone."""
    by_coupon = _rows(package, {"select": [_measure("order_count")], "group_by": [TYPE, COUPON]})
    reference = """
        SELECT i.product_type, c.coupon_id, COUNT(DISTINCT o.order_id) FROM orders o
        JOIN order_items i ON i.order_id = o.order_id LEFT JOIN coupons c ON c.code = o.coupon_code
        GROUP BY 1, 2
    """
    assert by_coupon == _reference(package, reference)
    assert ("beverage", None, 2) in by_coupon
    by_type: dict[str, int] = {}
    for product_type, _coupon, orders in by_coupon:
        by_type[product_type] = by_type.get(product_type, 0) + orders
    assert _rows(package, {"select": [_measure("order_count")], "group_by": [TYPE]}) == sorted(
        by_type.items()
    )


def test_a_lookup_joined_outside_exists_is_scanned_again_inside_it(package: Path) -> None:
    """Customers are joined for the group and read again on the path to sessions."""
    query = {
        "select": [_measure("order_count"), _measure("revenue")],
        "group_by": [SEGMENT],
        "where": [WEB],
    }
    result = _run(package, query)
    reference = f"""
        SELECT c.segment, COUNT(*), SUM(o.total) FROM orders o
        LEFT JOIN customers c ON c.customer_id = o.customer_id
        WHERE EXISTS ({_HAS_WEB_SESSION}) GROUP BY 1
    """
    assert (
        _normal(tuple(row.values()) for row in typed_rows(result))
        == _reference(package, reference)
        == [("retail", 2, 30.0), ("wholesale", 2, 30.0)]
    )
    # Each measure's leaf joins customers outside EXISTS and scans them inside it.
    sql = result["rendered_sql"]
    assert sql.count("LEFT JOIN customers ON") == sql.count("FROM customers\n") == 2


_OWN_ORDERS_SQL = "COUNT(DISTINCT CASE WHEN o.customer_id = 10 THEN o.order_id END)"
_OWN_REVENUE_SQL = "SUM(CASE WHEN o.customer_id = 10 THEN o.total END)"


# Orders with a beverage: 1 (customer 10, total 10, two beverages), 2 (10, 20), 4 (12, 40) and
# 6 (11, NULL total). A join to the items would count order 1 twice: a count of 4, a sum of
# 40, a retail sum of 80 and a difference of 50.
@pytest.mark.parametrize(
    ("expression", "value", "expected"),
    [
        (OWN_ORDERS_IF, _OWN_ORDERS_SQL, 2),
        (_if("count", _compare("total", ">=", 10)), "COUNT(CASE WHEN o.total >= 10 THEN 1 END)", 3),
        (OWN_REVENUE_IF, _OWN_REVENUE_SQL, 30),
        (
            _if("sum", _compare("segment", "=", "retail", "entity.hop_customer"), "total"),
            "SUM(CASE WHEN c.segment = 'retail' THEN o.total END)",
            70,
        ),
        (OWN_SHARE, f"1.0 * {_OWN_ORDERS_SQL} / COUNT(*)", 0.5),
        (
            {
                "kind": "arithmetic",
                "op": "subtract",
                "left": {"measure": "measure.hop.revenue"},
                "right": OWN_REVENUE_IF,
            },
            f"SUM(o.total) - {_OWN_REVENUE_SQL}",
            40,
        ),
    ],
    ids=["distinct_count", "count", "sum", "sum_by_lookup", "ratio", "arithmetic"],
)
def test_conditional_aggregates_filtered_by_a_child_count_each_parent_once(
    package: Path, expression: dict[str, Any], value: str, expected: float
) -> None:
    """Each aggregate_if leaf, and each operand of a ratio or arithmetic, keeps its rows with
    EXISTS as a measure's leaf does, so no child multiplies them."""
    query = {"select": [{"expression": expression, "as": "v"}], "where": [BEVERAGE]}
    result = _run(package, query)
    reference = f"""
        SELECT {value} FROM orders o LEFT JOIN customers c ON c.customer_id = o.customer_id
        WHERE EXISTS ({_HAS_BEVERAGE})
    """
    rows = _normal(tuple(row.values()) for row in typed_rows(result))
    assert rows == _reference(package, reference) == [(expected,)]
    sql = result["rendered_sql"]
    assert sql.count("EXISTS (") == (2 if expression["kind"] in {"ratio", "arithmetic"} else 1)
    assert "JOIN order_items" not in sql and "SELECT DISTINCT" not in sql


def test_conditional_aggregates_keep_null_groups_beside_a_child_filter(package: Path) -> None:
    """Grouped by coupon: orders 1 and 2 (customer 10, coupon 1) and the NULL group of orders 4
    (40) and 6 (NULL total). Coupon 1 has no other customer's order, so its sum reads 0 (the
    sum has amounts elsewhere), its average stays NULL and its share is 0."""
    others = _compare("customer_id", "!=", 10)
    others_revenue = _if("sum", others, "total")
    query = {
        "select": [
            {"expression": others_revenue, "as": "sum"},
            {"expression": _if("avg", others, "total"), "as": "average"},
            {
                "expression": _ratio(others_revenue, {"measure": "measure.hop.revenue"}),
                "as": "share",
            },
        ],
        "group_by": [COUPON],
        "where": [BEVERAGE],
    }
    others_sql = "CASE WHEN o.customer_id <> 10 THEN o.total END"
    reference = f"""
        SELECT c.coupon_id, COALESCE(SUM({others_sql}), 0), AVG({others_sql}),
          COALESCE(SUM({others_sql}), 0) / NULLIF(SUM(o.total), 0)
        FROM orders o LEFT JOIN coupons c ON c.code = o.coupon_code
        WHERE EXISTS ({_HAS_BEVERAGE}) GROUP BY 1
    """
    assert (
        _rows(package, query)
        == _reference(package, reference)
        == _normal([(1, 0.0, None, 0.0), (None, 40.0, 40.0, 1.0)])
    )


def test_a_conditional_distinct_count_grouped_by_a_child_dimension(package: Path) -> None:
    """Grouped across the hop, a conditional distinct count counts each order once in every
    product type it holds, as order_count does; order 2 holds both."""
    query = {"select": [{"expression": OWN_ORDERS_IF, "as": "v"}], "group_by": [TYPE]}
    reference = f"""
        SELECT i.product_type, {_OWN_ORDERS_SQL}
        FROM order_items i JOIN orders o ON o.order_id = i.order_id GROUP BY 1
    """
    assert (
        _rows(package, query) == _reference(package, reference) == [("beverage", 2), ("jaffle", 1)]
    )


@pytest.mark.parametrize(
    "query",
    [
        FILTERED_SUM,
        GROUPED_COUNT,
        {"select": [{"expression": OWN_REVENUE_IF, "as": "v"}], "where": [BEVERAGE]},
    ],
    ids=["filtered", "grouped", "conditional"],
)
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
    # Each de-duplicated leaf discloses itself.
    disclosed = _disclosed(_run(package, query))
    assert sorted(w["object_ids"][0] for w in disclosed) == [
        "measure.hop.buyer_count",
        "measure.hop.order_count",
    ]


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


def _bound_filter(clause: dict[str, Any]) -> dict[str, Any]:
    """Revenue with a measure-bound filter (the metric_filter path)."""
    expression = {
        "kind": "aggregate",
        "measure": "measure.hop.revenue",
        "aggregation": "sum",
        "filter": {"all": [clause]},
    }
    return {"select": [{"expression": expression, "as": "revenue"}]}


def _conditional(expression: dict[str, Any], **query: Any) -> dict[str, Any]:
    return {"select": [{"expression": expression, "as": "v"}], **query}


# Receipts are keyed by their order, but each payment is a row, as for the paid measure.
PAID_IF = _if(
    "sum", _compare("amount", ">", 0, "entity.hop_receipt"), "amount", "entity.hop_receipt"
)
NEGATED = "'has a row that is not X' and 'has no row that is X' differ"
TWO_CONDITIONS = "both cross a one-to-many hop"


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        # Order revenue by product type: split over the items, or each order's full total?
        ({"select": [_measure("revenue")], "group_by": [TYPE]}, "is ambiguous across"),
        ({"select": [_measure("revenue", "avg")], "group_by": [TYPE]}, "is ambiguous across"),
        # A measure's own filter has no child scope to state (query filters do: see below).
        (_bound_filter({"field": TYPE, "op": "!=", "value": "beverage"}), NEGATED),
        # A negated test with no exact complement has no 'none' reading to offer.
        (_where("IS DISTINCT FROM", "beverage"), NEGATED),
        (_where("NOT ILIKE", "bev%"), NEGATED),
        # At most one condition may cross a one-to-many hop: with two, one row may have to meet
        # both, or any rows each. On siblings under a shared hop, or both groups.
        ({**GROUPED_COUNT, "where": [{"field": HOT, "op": "=", "value": True}]}, TWO_CONDITIONS),
        (
            {**GROUPED_COUNT, "where": [{"field": CATEGORY, "op": "=", "value": "hot"}]},
            TWO_CONDITIONS,
        ),
        (
            {
                "select": [_measure("credit")],
                "where": [
                    {"field": TYPE, "op": "=", "value": "jaffle"},
                    {"field": METHOD, "op": "=", "value": "card"},
                ],
            },
            TWO_CONDITIONS,
        ),
        (
            {
                "select": [_measure("customer_count")],
                "group_by": [TYPE],
                "where": [{"field": METHOD, "op": "=", "value": "card"}],
            },
            TWO_CONDITIONS,
        ),
        ({"select": [_measure("customer_count")], "group_by": [TYPE, METHOD]}, TWO_CONDITIONS),
        # On a boolean only "= true" reads one way.
        (_bound_filter({"field": HOT, "op": "<", "value": True}), NEGATED),
        (_bound_filter({"field": HOT, "op": "=", "value": 0}), NEGATED),
        # orders -> customer -> sessions: many-to-many through the customer.
        ({"select": [_measure("order_count")], "group_by": [CHANNEL]}, "many-to-many"),
        # Grouping still requires the declared parent key, unlike filter-only EXISTS.
        ({"select": [_measure("face_value")], "group_by": [TYPE]}, "join off the declared key"),
        # Two payments of one receipt with equal amounts would merge into one row.
        ({"select": [_measure("paid")], "where": [BEVERAGE]}, "rows are finer than its entity"),
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
        # A conditional aggregate, alone or inside a ratio, refuses what its measure refuses.
        (_conditional(OWN_REVENUE_IF, group_by=[TYPE]), "is ambiguous across"),
        (_conditional(OWN_REVENUE_IF, where=[BEVERAGE, WEB]), TWO_CONDITIONS),
        (
            _conditional(
                OWN_SHARE, group_by=[TYPE], where=[{"field": HOT, "op": "=", "value": True}]
            ),
            TWO_CONDITIONS,
        ),
        (_conditional(OWN_ORDERS_IF, group_by=[CHANNEL]), "many-to-many"),
        (_conditional(PAID_IF, where=[BEVERAGE]), "rows are finer than its entity"),
        (
            _conditional(_ratio(PAID_IF, {"measure": "measure.hop.order_count"}), where=[BEVERAGE]),
            "rows are finer than its entity",
        ),
    ],
    ids=[
        "sum",
        "avg",
        "measure_filter",
        "is_distinct_from",
        "not_ilike",
        "group_and_filter_one_child",
        "group_and_filter_through_a_lookup",
        "sibling_filters",
        "group_and_sibling_filter",
        "sibling_groups",
        "measure_filter_boolean_less_than_true",
        "measure_filter_boolean_zero",
        "many_to_many",
        "off_key_join",
        "finer_row_grain",
        "non_additive",
        "cumulative",
        "conditional_grouped_sum",
        "conditional_two_filters",
        "conditional_ratio_group_and_filter",
        "conditional_many_to_many",
        "conditional_finer_row_grain",
        "conditional_ratio_finer_row_grain",
    ],
)
def test_ambiguous_shapes_stay_refused(package: Path, query: dict[str, Any], reason: str) -> None:
    error = _refusal(package, query)
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert reason in error["why_invalid"]
    assert error["recovery_hints"]


def _has_item(condition: str) -> str:
    return f"EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND {condition})"


def _negations(any_not: str, none: str) -> dict[str, str]:
    return {"any_not": _has_item(any_not), "none": "NOT " + _has_item(none)}


@pytest.mark.parametrize(
    ("query", "readings"),
    [
        # "Orders without a beverage" and "orders with a non-beverage item" differ.
        (
            _where("!=", "beverage"),
            _negations("i.product_type != 'beverage'", "i.product_type = 'beverage'"),
        ),
        (
            _where("<>", "beverage"),
            _negations("i.product_type <> 'beverage'", "i.product_type = 'beverage'"),
        ),
        (
            _where("NOT IN", ["beverage"]),
            _negations("i.product_type NOT IN ('beverage')", "i.product_type IN ('beverage')"),
        ),
        (
            _where("NOT LIKE", "bev%"),
            _negations("i.product_type NOT LIKE 'bev%'", "i.product_type LIKE 'bev%'"),
        ),
        (
            _where("IS NULL", None),
            _negations("i.product_type IS NULL", "i.product_type IS NOT NULL"),
        ),
        (_where("=", None), _negations("i.product_type IS NULL", "i.product_type IS NOT NULL")),
        # On a boolean only "= true" reads one way.
        (_where("=", False, HOT), _negations("i.is_hot = false", "i.is_hot != false")),
        (_where("=", "false", HOT), _negations("i.is_hot = false", "i.is_hot != false")),
        (_where("in", [False], HOT), _negations("i.is_hot IN (false)", "i.is_hot NOT IN (false)")),
        (_where("<", True, HOT), _negations("i.is_hot < true", "i.is_hot >= true")),
        (_where("<=", False, HOT), _negations("i.is_hot <= false", "i.is_hot > false")),
        # One item that is both, or a beverage and some jaffle?
        (
            {"select": [_measure("revenue")], "where": [BEVERAGE, {**BEVERAGE, "value": "jaffle"}]},
            {
                "same_row": _has_item("i.product_type = 'beverage' AND i.product_type = 'jaffle'"),
                "separate_rows": _has_item("i.product_type = 'beverage'")
                + " AND "
                + _has_item("i.product_type = 'jaffle'"),
            },
        ),
    ],
    ids=[
        "not_equal",
        "angle_not_equal",
        "not_in",
        "not_like",
        "is_null",
        "equals_null",
        "boolean_false",
        "boolean_false_text",
        "boolean_in_false",
        "boolean_less_than_true",
        "boolean_at_most_false",
        "two_filters_one_child",
    ],
)
def test_query_filters_that_leave_the_child_scope_unsaid_ask_for_it(
    package: Path, query: dict[str, Any], readings: dict[str, str]
) -> None:
    """Each reading is offered as a whole where list, which then answers as its reference."""
    error = _refusal(package, query)
    assert error["code"] == "AMBIGUOUS_CHILD_SCOPE"
    offered = error["details"]["clarification"]["options"]
    assert [option["id"] for option in offered] == list(readings)
    for option in offered:
        reference = f"SELECT SUM(o.total) FROM orders o WHERE {readings[option['id']]}"
        resent = {**query, "where": option["where"]}
        assert _rows(package, resent) == _reference(package, reference), option["id"]


@pytest.mark.parametrize(
    ("query", "reference", "readings"),
    [
        (
            _conditional(OWN_REVENUE_IF, where=[{**BEVERAGE, "op": "!="}]),
            "SELECT SUM(CASE WHEN o.customer_id = 10 THEN o.total END) FROM orders o WHERE {}",
            _negations("i.product_type != 'beverage'", "i.product_type = 'beverage'"),
        ),
        (
            _conditional(OWN_SHARE, where=[{**BEVERAGE, "op": "NOT IN", "value": ["x"]}]),
            "SELECT COUNT(DISTINCT CASE WHEN o.customer_id = 10 THEN o.order_id END) * 1.0 "
            "/ COUNT(DISTINCT o.order_id) FROM orders o WHERE {}",
            _negations("i.product_type NOT IN ('x')", "i.product_type IN ('x')"),
        ),
    ],
    ids=["conditional_not_equal", "conditional_ratio_not_in"],
)
def test_a_conditional_aggregate_asks_about_a_negated_child_filter_too(
    package: Path, query: dict[str, Any], reference: str, readings: dict[str, str]
) -> None:
    """Every leaf of an expression reads the child scope the same way, operand by operand."""
    error = _refusal(package, query)
    assert error["code"] == "AMBIGUOUS_CHILD_SCOPE"
    offered = error["details"]["clarification"]["options"]
    assert [option["id"] for option in offered] == list(readings)
    for option in offered:
        resent = {**query, "where": option["where"]}
        expected = _reference(package, reference.format(readings[option["id"]]))
        assert _rows(package, resent) == expected, option["id"]


@pytest.mark.parametrize("op", ["IS", "IS NOT"])
def test_invalid_is_operand_is_refused_before_hop_analysis(package: Path, op: str) -> None:
    error = _refusal(package, _where(op, "beverage"))
    assert error["code"] == "INVALID_QUERY"
    assert error["recovery_hints"][0]["code"] == "USE_EQUALITY_FOR_SCALAR"


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


# jaffle_shop's item -> order relationship is rollup_safe, so a grouped order count takes the
# entity_in_terms_of rewrite. The values its tests/advanced.yml pins come from this SQL on the
# seeded data/jaffle_shop.duckdb:
#   orders_with_a_beverage_by_store_snapshot (Brooklyn 20117, Philadelphia 35857) and
#   revenue_of_orders_with_a_beverage_by_store_snapshot (238963.11, 452138.42):
#     SELECT s.store_name, COUNT(*), ROUND(SUM(o.order_total_cents) / 100.0, 2)
#     FROM jaffle_order o JOIN jaffle_store s ON s.store_id = o.store_id
#     WHERE EXISTS (SELECT 1 FROM jaffle_item i
#                   WHERE i.order_id = o.order_id AND i.product_type = 'beverage')
#     GROUP BY 1
#   ordering_customers_by_item_product_type_snapshot (beverage 939, jaffle 678):
#     SELECT i.product_type, COUNT(DISTINCT o.customer_id)
#     FROM jaffle_item i JOIN jaffle_order o ON o.order_id = i.order_id GROUP BY 1
def test_rollup_safe_package_discloses_each_crossing_leaf(runtime_factory) -> None:
    """With two measures, the order count's entity_in_terms_of rewrite is still disclosed."""
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
                {"expression": {"measure": "measure.jaffle.item_revenue_usd"}, "as": "revenue"},
            ],
            "group_by": ["dimension.jaffle_item_product_type"],
        }
        result = runtime.query(query)
        steps = [step["kind"] for step in runtime.validate(query)["logical_plan"]["rewrite_steps"]]
        db_path = runtime.db_path
    finally:
        runtime.close()
    assert steps == ["entity_in_terms_of"]
    assert [w["code"] for w in result["warnings"]].count("REWRITE_APPLIED") == 1
    assert result["provenance_summary"]["rewrite_status"] == "rewritten"
    with duckdb.connect(db_path, read_only=True) as conn:
        expected = conn.execute(
            "SELECT product_type, COUNT(DISTINCT order_id), SUM(item_revenue_cents) / 100.0"
            " FROM jaffle_item GROUP BY 1"
        ).fetchall()
    assert _normal(tuple(row.values()) for row in typed_rows(result)) == _normal(expected)


# A customer's items and sessions: the items of its orders and the sessions it held (not the
# items or sessions of orders its sessions converted to).
_CUSTOMER_CHILD_PINS = [
    PathPreferenceConfig(
        "entity.jaffle_customer",
        "entity.jaffle_item",
        ["relationship.orders_customer", "relationship.order_items_order"],
    ),
    PathPreferenceConfig(
        "entity.jaffle_customer",
        "entity.jaffle_storefront_session",
        ["relationship.storefront_sessions_customer"],
    ),
]


@pytest.mark.parametrize(
    ("measure", "groups", "pins"),
    [
        # Two groups on the same child: items.
        (
            "measure.jaffle.order_count",
            ["dimension.jaffle_item_product_type", "dimension.jaffle_item_product_name"],
            [],
        ),
        # Two groups on different children of a customer: its orders' items and its sessions.
        (
            "measure.jaffle.customer_count",
            ["dimension.jaffle_item_product_type", "dimension.jaffle_storefront_session_store_id"],
            _CUSTOMER_CHILD_PINS,
        ),
    ],
    ids=["same_child", "different_children"],
)
def test_rollup_safe_package_refuses_two_groups_across_a_hop(
    runtime_factory, measure: str, groups: list[str], pins: list[PathPreferenceConfig]
) -> None:
    """Grouping a distinct count by two dimensions across one-to-many hops was answered before;
    a query may now group or filter across a one-to-many hop once. Both refusal sites raise the
    same error, so the entity_in_terms_of branch is told apart by each group alone planning
    through it."""
    runtime = runtime_factory("jaffle_shop")
    if pins:
        config = replace(runtime.config, path_preferences=[*runtime.config.path_preferences, *pins])
        runtime.close()
        runtime = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def ask(group_by: list[str]) -> dict[str, Any]:
        select = [{"expression": {"measure": measure}, "as": "value"}]
        return runtime.validate({"version": 1, "select": select, "group_by": group_by})

    try:
        report = ask(groups)
        alone = [ask([group]) for group in groups]
    finally:
        runtime.close()
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert "both cross a one-to-many hop" in error["why_invalid"]
    for single in alone:
        assert [step["kind"] for step in single["logical_plan"]["rewrite_steps"]] == [
            "entity_in_terms_of"
        ]


# The de-duplicated leaf on every locally testable warehouse: a CTE, SELECT DISTINCT and an
# aggregate over its columns. Only the time bucket and the median differ by dialect.
WAREHOUSES = ("duckdb", "postgres", "clickhouse", "ducklake")
_TRUNC = "DATE_TRUNC('month', CAST(orders.ordered_at AS TIMESTAMP))"
MONTH = {
    "duckdb": _TRUNC,
    "postgres": _TRUNC,
    "clickhouse": f"CAST({_TRUNC} AS TIMESTAMP)",
    "ducklake": _TRUNC,
}
_VALUE = "orders.total"
MEDIAN = {
    "duckdb": f"MEDIAN({_VALUE})",
    "postgres": f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {_VALUE} ASC)",
    "clickhouse": f"quantileExactInclusive(0.5)({_VALUE})",
    "ducklake": f"MEDIAN({_VALUE})",
}
SHAPES = {
    "filtered": FILTERED_SUM,
    "grouped": GROUPED_COUNT,
    "monthly": {**FILTERED_SUM, "time": {"temporal_role": ROLE, "grain": "month"}},
    "median": {
        "select": [
            {
                "expression": {"measure": "measure.hop.revenue", "aggregation": "median"},
                "as": "median_revenue",
            }
        ],
        "where": [BEVERAGE],
    },
}
DIALECT_SQL = {
    "filtered": """WITH leaf_1 AS (
SELECT
  SUM(orders.total) AS m1,
  COUNT(1) AS m1_rows
FROM orders
WHERE
  EXISTS (
SELECT
  1 AS match
FROM order_items
WHERE
  orders.order_id = order_items.order_id
  AND order_items.product_type = 'beverage'
)
),
guarded_base AS (
SELECT
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.m1 AS revenue
FROM guarded_base AS base""",
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
),
guarded_base AS (
SELECT
  base.g1 AS g1,
  CASE WHEN MAX(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1
FROM leaf_1 AS base
)
SELECT
  base.g1 AS "dimension.hop_item_product_type",
  base.m1 AS orders
FROM guarded_base AS base""",
    "monthly": """WITH leaf_1 AS (
SELECT
  {month} AS t,
  SUM(orders.total) AS m1,
  COUNT(1) AS m1_rows
FROM orders
WHERE
  EXISTS (
SELECT
  1 AS match
FROM order_items
WHERE
  orders.order_id = order_items.order_id
  AND order_items.product_type = 'beverage'
)
GROUP BY
  {month}
),
guarded_base AS (
SELECT
  base.t AS t,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.t AS "temporal_role.hop_order_ordered_at__month",
  base.m1 AS revenue
FROM guarded_base AS base
ORDER BY
  "temporal_role.hop_order_ordered_at__month" ASC""",
    "median": """WITH leaf_1 AS (
SELECT
  {median} AS m1
FROM orders
WHERE
  EXISTS (
SELECT
  1 AS match
FROM order_items
WHERE
  orders.order_id = order_items.order_id
  AND order_items.product_type = 'beverage'
)
)
SELECT
  base.m1 AS median_revenue
FROM leaf_1 AS base""",
}


CLICKHOUSE_SQL = {
    "filtered": """WITH leaf_1__leaf_1_entity_rows AS (
SELECT DISTINCT
  orders.order_id AS __entity_key_1,
  orders.total AS __entity_value,
  1 AS __entity_rows
FROM orders
INNER JOIN order_items ON orders.order_id = order_items.order_id
WHERE
  order_items.product_type = 'beverage'
),
leaf_1 AS (
SELECT
  SUM(leaf_1__leaf_1_entity_rows.__entity_value) AS m1,
  COUNT(leaf_1__leaf_1_entity_rows.__entity_rows) AS m1_rows
FROM leaf_1__leaf_1_entity_rows
),
guarded_base AS (
SELECT
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.m1 AS revenue
FROM guarded_base AS base""",
    "monthly": """WITH leaf_1__leaf_1_entity_rows AS (
SELECT DISTINCT
  orders.order_id AS __entity_key_1,
  {month} AS t,
  orders.total AS __entity_value,
  1 AS __entity_rows
FROM orders
INNER JOIN order_items ON orders.order_id = order_items.order_id
WHERE
  order_items.product_type = 'beverage'
),
leaf_1 AS (
SELECT
  leaf_1__leaf_1_entity_rows.t AS t,
  SUM(leaf_1__leaf_1_entity_rows.__entity_value) AS m1,
  COUNT(leaf_1__leaf_1_entity_rows.__entity_rows) AS m1_rows
FROM leaf_1__leaf_1_entity_rows
GROUP BY
  leaf_1__leaf_1_entity_rows.t
),
guarded_base AS (
SELECT
  base.t AS t,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.t AS "temporal_role.hop_order_ordered_at__month",
  base.m1 AS revenue
FROM guarded_base AS base
ORDER BY
  "temporal_role.hop_order_ordered_at__month" ASC""",
    "median": """WITH leaf_1__leaf_1_entity_rows AS (
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
  {median} AS m1
FROM leaf_1__leaf_1_entity_rows
)
SELECT
  base.m1 AS median_revenue
FROM leaf_1 AS base""",
}


@pytest.mark.parametrize("warehouse", WAREHOUSES)
@pytest.mark.parametrize("shape", SHAPES)
def test_each_dialect_renders_the_de_duplicated_leaf(
    package: Path, warehouse: str, shape: str
) -> None:
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    sql = compile_query(config, Registry(config), {"version": 1, **SHAPES[shape]})["sql"]
    templates = CLICKHOUSE_SQL if warehouse == "clickhouse" and shape != "grouped" else DIALECT_SQL
    value = (
        "leaf_1__leaf_1_entity_rows.__entity_value" if warehouse == "clickhouse" else "orders.total"
    )
    median = MEDIAN[warehouse].replace("orders.total", value)
    expected = templates[shape].format(month=MONTH[warehouse], median=median)
    # ClickHouse reads an unmatched outer-join field as NULL only with this setting.
    assert sql == expected + ("\nSETTINGS join_use_nulls = 1" if warehouse == "clickhouse" else "")


@pytest.mark.parametrize("warehouse", ["snowflake", "bigquery", "databricks"])
def test_remote_dialects_render_the_filter_as_correlated_exists(
    package: Path, warehouse: str
) -> None:
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    sql = compile_query(config, Registry(config), {"version": 1, **FILTERED_SUM})["sql"]
    assert sql == DIALECT_SQL["filtered"]
