"""A query says whether conditions on child rows apply to the same row.

"Customers with an item that is a beverage and costs over 5" may mean one item that is both
(``same_row``) or a beverage and some item over 5 (``separate_rows``). An entity filter,
``{entity, any | none}``, states it: one EXISTS over the conjunction (``any``) or NOT EXISTS
(``none``). Two or more positive flat filters on one child, or one negated one, are refused
with a clarification whose two options are complete rewritten ``where`` lists, offered only
when both answer. Every other shape keeps its refusal; a query states one child scope. Every
answer is checked on DuckDB against reference SQL written independently of the engine.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import route_pin
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner.plan import plan_payload
from semantic_rails.policies import row_filters_for_context
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import (
    AggregateRelationConfig,
    PathPreferenceConfig,
    SemanticPolicyConfig,
)
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.result_helpers import typed_rows

# Customer 1: a cheap beverage and a jaffle over 5 in one order. 2: one beverage over 5.
# 3: no orders. 4: one beverage with a NULL price, and no region. 5: an order with no items.
# 6: a cheap beverage in one order and a jaffle over 5 in another, and a region the regions
# table lacks. 7: two beverages over 5 in one order and one more in another (the double-count
# trap). Item 999 belongs to no order. Product 'mystery' has no category row.
SEED = """
CREATE TABLE customers AS SELECT * FROM (VALUES
  (1, 'n', 100.0), (2, 's', 200.0), (3, 'n', 300.0), (4, NULL, 400.0),
  (5, 's', 500.0), (6, 'e', 600.0), (7, 'n', 700.0)
) AS t(customer_id, region_code, credit);
CREATE TABLE regions AS SELECT * FROM (VALUES ('n', 'North'), ('s', 'South'))
  AS t(region_code, region_name);
CREATE TABLE orders AS SELECT * FROM (VALUES
  (10, 1, 20.0), (20, 2, 6.0), (40, 4, 9.0), (50, 5, 1.0), (60, 6, 3.0), (61, 6, 17.0),
  (70, 7, 30.0), (71, 7, 8.0)
) AS t(order_id, customer_id, total);
CREATE TABLE items AS SELECT * FROM (VALUES
  (101, 10, 'cola', 'beverage', 3.0), (102, 10, 'toast', 'jaffle', 17.0),
  (201, 20, 'cola', 'beverage', 6.0), (401, 40, 'tea', 'beverage', NULL),
  (601, 60, 'cola', 'beverage', 3.0), (611, 61, 'toast', 'jaffle', 17.0),
  (701, 70, 'tea', 'beverage', 8.0), (702, 70, 'cola', 'beverage', 9.0),
  (711, 71, 'mystery', 'beverage', 7.0), (999, 99, 'cola', 'beverage', 50.0)
) AS t(item_id, order_id, sku, product_type, price);
CREATE TABLE products AS SELECT * FROM (VALUES ('cola', 'soda'), ('tea', 'hot'), ('toast', 'food'))
  AS t(sku, category);
CREATE TABLE sessions AS SELECT * FROM (VALUES (1, 1, 'web'), (2, 2, 'app'), (3, 6, 'web'), (4, 7, NULL))
  AS t(session_id, customer_id, channel);
"""

PACKAGE = """
schema_version: 1
package: {id: scope, namespace: scope, warehouse: duckdb, default_db: data/warehouse.duckdb,
  seed: {kind: sql_script, source: data/seed.sql}}
"""
MODELS = {
    "customers": """
model:
  id: customers
  relation: customers
  entities: {customer: {}, region: {expr: region_code}}
  measures:
    customer_count: {label: Customers, kind: entity_count, entity_key: customer_id,
      accumulation: {kind: event}, value_type: count}
    credit: {label: Credit, kind: aggregate, expr: credit, accumulation: {kind: flow},
      value_type: currency}
""",
    "regions": """
model:
  id: regions
  relation: regions
  entities: {region: {}}
  dimensions:
    region_name: {label: Region, kind: categorical}
""",
    "orders": """
model:
  id: orders
  relation: orders
  entities: {order: {}, customer: {}}
  measures:
    order_count: {label: Orders, kind: entity_count, entity_key: order_id,
      accumulation: {kind: event}, value_type: count}
    revenue: {label: Revenue, kind: aggregate, expr: total, accumulation: {kind: flow},
      value_type: currency}
""",
    "items": """
model:
  id: items
  relation: items
  entities: {item: {}, order: {}, product: {expr: sku}}
  dimensions:
    product_type: {label: Product type, kind: categorical}
    price: {label: Price, kind: number}
  measures:
    item_revenue: {label: Item revenue, kind: aggregate, expr: price,
      accumulation: {kind: flow}, value_type: currency}
""",
    "products": """
model:
  id: products
  relation: products
  entities: {product: {}}
  dimensions:
    category: {label: Category, kind: categorical}
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
    "customer": ["customer_id", "customers", "Customer"],
    "region": ["region_code", "regions", "Region"],
    "order": ["order_id", "orders", "Order"],
    "item": ["item_id", "items", "Order item"],
    "product": ["sku", "products", "Product"],
    "session": ["session_id", "sessions", "Session"],
}
METRICS = """
metrics:
  scope.customers:
    as: metric.scope.customers
    label: Customers
    kind: aggregate
    measure: customer_count
    aggregation: count_distinct
    value_type: count
"""

ITEM = "entity.scope_item"
SESSION = "entity.scope_session"
PAYMENT = "entity.scope_payment"
TYPE = "dimension.scope_item_product_type"
PRICE = "dimension.scope_item_price"
CATEGORY = "dimension.scope_product_category"
CHANNEL = "dimension.scope_session_channel"
REGION = "dimension.scope_region_region_name"
CUSTOMER_ID = "dimension.scope_customer_id"
BEVERAGE = {"field": TYPE, "op": "is", "value": "beverage"}
OVER_5 = {"field": PRICE, "op": "gt", "value": 5}
WEB = {"field": CHANNEL, "op": "is", "value": "web"}
CARD = {"field": "dimension.scope_payment_method", "op": "is", "value": "card"}
SAME_ROW = [{"entity": ITEM, "any": [BEVERAGE, OVER_5]}]
SEPARATE_ROWS = [{"entity": ITEM, "any": [BEVERAGE]}, {"entity": ITEM, "any": [OVER_5]}]
NO_SAME_ROW = [{"entity": ITEM, "none": [BEVERAGE, OVER_5]}]

# Reference SQL, written without the engine: a customer's items are its orders' items.
HAS_ITEM = (
    "EXISTS (SELECT 1 FROM orders o JOIN items i ON i.order_id = o.order_id "
    "WHERE o.customer_id = c.customer_id AND {})"
)
SAME_ROW_SQL = HAS_ITEM.format("i.product_type = 'beverage' AND i.price > 5")
SEPARATE_ROWS_SQL = (
    HAS_ITEM.format("i.product_type = 'beverage'") + " AND " + HAS_ITEM.format("i.price > 5")
)
NO_SAME_ROW_SQL = "NOT " + SAME_ROW_SQL
NOT_BEVERAGE = {"field": TYPE, "op": "not", "value": "beverage"}
NOT_17 = {"field": PRICE, "op": "not", "value": 17}
UNDER_5 = {"field": PRICE, "op": "lt", "value": 5}
UNDER_10 = {"field": PRICE, "op": "lt", "value": 10}


def _any(*conditions: dict[str, Any]) -> dict[str, Any]:
    return {"entity": ITEM, "any": list(conditions)}


def _none(*conditions: dict[str, Any]) -> dict[str, Any]:
    return {"entity": ITEM, "none": list(conditions)}


def _write_package(
    root: Path, seed: str, models: dict[str, str], graph: dict[str, list[str]] = ENTITIES
) -> Path:
    entities = {
        name: {"label": label, "key": [key], "model": model}
        for name, (key, model, label) in graph.items()
    }
    files = {
        "package.yml": PACKAGE,
        "data/seed.sql": seed,
        "graph.yml": yaml.safe_dump({"graph": {"entities": entities}}),
        "metrics/core.yml": METRICS,
        **{f"models/{name}.yml": text for name, text in models.items()},
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


# One runtime per package for the module, opened (and the warehouse seeded) by its fixture.
_ENGINES: dict[Path, Runtime] = {}


def _served(root: Path) -> Iterator[Path]:
    _ENGINES[root] = opened(Runtime.from_path(str(root)))
    yield root
    _ENGINES.pop(root).close()


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    yield from _served(_write_package(tmp_path_factory.mktemp("scope") / "scope", SEED, MODELS))


@pytest.fixture(scope="module")
def item_customer_package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Items also carry a customer key, and every item names customer 3 (who has no orders):
    a customer reaches items through its orders or directly, and the two disagree."""
    items = "CREATE TABLE items AS SELECT * FROM"
    seed = SEED.replace(items, "CREATE TABLE items AS SELECT *, 3 AS customer_id FROM")
    models = {
        **MODELS,
        "items": MODELS["items"].replace(
            "product: {expr: sku}}", "product: {expr: sku}, customer: {}}"
        ),
    }
    assert seed != SEED and models["items"] != MODELS["items"]
    yield from _served(_write_package(tmp_path_factory.mktemp("scope") / "scope", seed, models))


@pytest.fixture(scope="module")
def payments_package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Orders also have payments, a sibling of items. Card pays order 10 (customer 1, whose
    beverage and item over 5 are on it too) and order 61 (customer 6, whose beverage is on
    order 60); cash pays order 20."""
    seed = SEED + (
        "CREATE TABLE payments AS SELECT * FROM (VALUES (1, 10, 'card'), (2, 61, 'card'), "
        "(3, 20, 'cash')) AS t(payment_id, order_id, method);\n"
    )
    payments = """
model:
  id: payments
  relation: payments
  entities: {payment: {}, order: {}}
  dimensions:
    method: {label: Method, kind: categorical}
"""
    graph = {**ENTITIES, "payment": ["payment_id", "payments", "Payment"]}
    root = _write_package(
        tmp_path_factory.mktemp("scope") / "scope", seed, {**MODELS, "payments": payments}, graph
    )
    yield from _served(root)


def _measure(name: str, alias: str = "") -> dict[str, Any]:
    return {"expression": f"measure.scope.{name}", "as": alias or name}


def _query(where: list[Any], *measures: str, **extra: Any) -> dict[str, Any]:
    names = measures or ("customer_count",)
    return {"select": [_measure(name) for name in names], "where": where, **extra}


def _engine(config: Any, query: dict[str, Any]) -> dict[str, Any]:
    """The compiler's own form of a call."""
    return read_call(query, config=config)[0]


def _run(package: Path, query: dict[str, Any], *, validate: bool = False) -> dict[str, Any]:
    engine = _ENGINES[package]
    return engine.validate(query) if validate else engine.query(query)


def test_a_misspelled_child_value_is_named(package: Path) -> None:
    for match in ("any", "none"):
        where = [{"entity": ITEM, match: [{**BEVERAGE, "value": "beverag"}]}]
        response = _run(package, _query(where))
        (warning,) = [w for w in response["warnings"] if w["code"] == "FILTER_VALUE_NOT_FOUND"]
        assert warning["details"]["filters"] == [
            {"dimension": TYPE, "value": "beverag", "suggestion": "beverage"},
        ]


def _normal(rows: Any) -> list[tuple[Any, ...]]:
    return sorted(
        (
            tuple(round(float(v), 6) if isinstance(v, (float, Decimal)) else v for v in row)
            for row in rows
        ),
        key=lambda row: [str(v) for v in row],
    )


def _rows(package: Path, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    result = _run(package, query)
    assert result.get("ok", True), result
    return _normal(tuple(row.values()) for row in typed_rows(result))


def _reference(package: Path, sql: str) -> list[tuple[Any, ...]]:
    # On the runtime's own connection: one process opens a file with one configuration.
    with _ENGINES[package]._get_adapter()._db.conn.cursor() as cursor:
        return _normal(cursor.execute(sql).fetchall())


def _customers(package: Path, condition: str) -> list[tuple[Any, ...]]:
    return _reference(package, f"SELECT COUNT(*) FROM customers c WHERE {condition}")


def _refusal(package: Path, query: dict[str, Any]) -> dict[str, Any]:
    report = _run(package, query, validate=True)
    assert report["ok"] is False, report
    return report["errors"][0]


def _who(package: Path, where: list[Any]) -> list[int]:
    """The customers a where list keeps, by id."""
    rows = _rows(package, {**_query(where), "group_by": [CUSTOMER_ID]})
    return sorted(row[0] for row in rows if row[1])


def _ids(package: Path, condition: str) -> list[int]:
    """The customers reference SQL keeps, by id."""
    rows = _reference(package, f"SELECT c.customer_id FROM customers c WHERE {condition}")
    return sorted(row[0] for row in rows)


def _options(error: dict[str, Any]) -> dict[str, list[Any]]:
    return {
        option["id"]: option["where"] for option in error["details"]["clarification"]["options"]
    }


@pytest.mark.parametrize(
    ("where", "reference", "kept"),
    [
        (SAME_ROW, SAME_ROW_SQL, [2, 7]),
        (SEPARATE_ROWS, SEPARATE_ROWS_SQL, [1, 2, 6, 7]),
        (NO_SAME_ROW, NO_SAME_ROW_SQL, [1, 3, 4, 5, 6]),
    ],
    ids=["same_row", "separate_rows", "none"],
)
def test_the_scope_decides_which_customers_count(
    package: Path, where: list[Any], reference: str, kept: list[int]
) -> None:
    """Customer 1's beverage is cheap and its jaffle costs over 5: separate rows only. Customer
    2's one beverage over 5 counts for both. Customer 3, with no items, counts for neither and
    for 'none'; so does customer 5, whose one order has no items."""
    assert _rows(package, _query(where)) == _customers(package, reference) == [(len(kept),)]
    assert _who(package, where) == kept


def test_a_null_price_fails_the_comparison_inside_any_and_never_excludes_under_none(
    package: Path,
) -> None:
    """Customer 4's only item has a NULL price: not an item over 5, and not one that excludes."""
    over_5 = [{"entity": ITEM, "any": [OVER_5]}]
    none_over_5 = [{"entity": ITEM, "none": [OVER_5]}]
    assert 4 not in _who(package, over_5)
    assert 4 in _who(package, none_over_5)
    assert _rows(package, _query(over_5)) == _customers(package, HAS_ITEM.format("i.price > 5"))
    assert _rows(package, _query(none_over_5)) == _customers(
        package, "NOT " + HAS_ITEM.format("i.price > 5")
    )


def test_a_parent_sum_counts_each_customer_once(package: Path) -> None:
    """Customer 7 has three items that match; its credit is added once."""
    for where, condition in [(SAME_ROW, SAME_ROW_SQL), (SEPARATE_ROWS, SEPARATE_ROWS_SQL)]:
        reference = f"SELECT SUM(c.credit) FROM customers c WHERE {condition}"
        assert _rows(package, _query(where, "credit")) == _reference(package, reference)
    assert _rows(package, _query(SAME_ROW, "credit")) == [(900.0,)]
    naive = (
        "SELECT SUM(c.credit) FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
        "JOIN items i ON i.order_id = o.order_id WHERE i.product_type = 'beverage' AND i.price > 5"
    )
    assert _reference(package, naive) == [(2300.0,)]  # what a plain join would have said


def test_a_ratio_and_a_second_measure_entity_each_apply_the_group(package: Path) -> None:
    """Each leaf reads the group from its own entity: customers, and orders."""
    ratio = {
        "kind": "ratio",
        "numerator": "measure.scope.credit",
        "denominator": "measure.scope.customer_count",
    }
    query = {
        "select": [{"expression": ratio, "as": "credit_per_customer"}, _measure("order_count")],
        "where": SEPARATE_ROWS,
    }
    orders_with = (
        "SELECT COUNT(*) FROM orders o WHERE "
        "EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.order_id "
        "AND i.product_type = 'beverage') AND EXISTS (SELECT 1 FROM items i "
        "WHERE i.order_id = o.order_id AND i.price > 5)"
    )
    reference = f"""
        SELECT (SELECT SUM(c.credit) / COUNT(*) FROM customers c WHERE {SEPARATE_ROWS_SQL}),
               ({orders_with})
    """
    assert _rows(package, query) == _reference(package, reference) == [(400.0, 4)]


def test_grouped_by_a_parent_lookup_the_null_group_stays(package: Path) -> None:
    """Customer 6's region is not in the regions table: its row reads a NULL region."""
    query = {**_query(SEPARATE_ROWS, "customer_count", "credit"), "group_by": [REGION]}
    reference = f"""
        SELECT r.region_name, COUNT(*), SUM(c.credit) FROM customers c
        LEFT JOIN regions r ON r.region_code = c.region_code WHERE {SEPARATE_ROWS_SQL} GROUP BY 1
    """
    assert (
        _rows(package, query)
        == _reference(package, reference)
        == _normal([("North", 2, 800.0), ("South", 1, 200.0), (None, 1, 600.0)])
    )


def test_a_condition_through_a_lookup_from_the_child_reads_null_when_it_finds_no_row(
    package: Path,
) -> None:
    """Item 711's product has no category row: it is not 'hot', and its category IS NULL."""
    has = (
        "EXISTS (SELECT 1 FROM orders o JOIN items i ON i.order_id = o.order_id "
        "LEFT JOIN products p ON p.sku = i.sku WHERE o.customer_id = c.customer_id AND {})"
    )
    for condition, sql in [
        ({"field": CATEGORY, "op": "is", "value": "hot"}, "p.category = 'hot'"),
        ({"field": CATEGORY, "op": "missing", "value": True}, "p.category IS NULL"),
    ]:
        where = [{"entity": ITEM, "any": [BEVERAGE, condition]}]
        predicate = f"i.product_type = 'beverage' AND {sql}"
        assert _rows(package, _query(where)) == _customers(package, has.format(predicate))
    null_category = [{"entity": ITEM, "any": [{"field": CATEGORY, "op": "missing", "value": True}]}]
    assert _who(package, null_category) == [7]


def test_a_lookup_key_the_child_holds_is_read_from_the_child(package: Path) -> None:
    """The product key is the item's own sku column: no lookup join inside EXISTS."""
    tea = {"field": "dimension.scope_product_sku", "op": "is", "value": "tea"}
    where = [{"entity": ITEM, "any": [tea, OVER_5]}]
    result = _run(package, _query(where))
    assert "JOIN products" not in result["rendered_sql"]
    assert _normal(tuple(row.values()) for row in typed_rows(result)) == _customers(
        package, HAS_ITEM.format("i.sku = 'tea' AND i.price > 5")
    )


def test_the_restatement_spells_out_a_group() -> None:
    from semantic_rails.cli.interpretation import describe_query

    labels = {ITEM: "Order item", TYPE: "Product type", PRICE: "Price"}
    text = describe_query(_query(SAME_ROW + NO_SAME_ROW), labels)
    some, _, none = text.partition("where some Order item has Product type ")[2].partition(
        " and no Order item has Product type "
    )
    assert "beverage" in some and "Price" in some and "5" in some, text
    assert "beverage" in none and "Price" in none and "5" in none, text
    assert "{" not in text


# ---- Flat forms -----------------------------------------------------------------------


def test_two_flat_conditions_on_one_child_ask_for_the_scope(package: Path) -> None:
    region = {"field": REGION, "op": "is", "value": "North"}
    query = _query([region, BEVERAGE, OVER_5])
    error = _refusal(package, query)
    assert error["code"] == "AMBIGUOUS_CHILD_SCOPE"
    clarification = error["details"]["clarification"]
    assert clarification["kind"] == "child_scope"
    assert clarification["apply"] == ["query"]
    assert "Order item" in clarification["question"]
    meanings = {option["id"]: option["meaning"] for option in clarification["options"]}
    assert "Product type" in meanings["same_row"]
    assert [hint["option"] for hint in error["recovery_hints"]] == ["same_row", "separate_rows"]
    # Each option is the whole where, the other items unchanged.
    options = _options(error)
    assert options == {"same_row": [region, *SAME_ROW], "separate_rows": [region, *SEPARATE_ROWS]}
    assert error["details"]["paths"] == ["where[1]", "where[2]"]
    # Resending an option's where verbatim gives that option's answer.
    north = "c.region_code IN (SELECT region_code FROM regions WHERE region_name = 'North')"
    for option, sql in [("same_row", SAME_ROW_SQL), ("separate_rows", SEPARATE_ROWS_SQL)]:
        resent = {**query, "where": options[option]}
        assert _rows(package, resent) == _customers(package, f"{north} AND {sql}"), option


def test_a_flat_condition_through_a_lookup_from_the_child_shares_its_scope(
    package: Path,
) -> None:
    hot = {"field": CATEGORY, "op": "is", "value": "hot"}
    query = _query([BEVERAGE, hot])
    error = _refusal(package, query)
    assert error["code"] == "AMBIGUOUS_CHILD_SCOPE"
    options = _options(error)
    assert options == {
        "same_row": [_any(BEVERAGE, hot)],
        "separate_rows": [_any(BEVERAGE), _any(hot)],
    }
    has = (
        "EXISTS (SELECT 1 FROM orders o JOIN items i ON i.order_id = o.order_id "
        "LEFT JOIN products p ON p.sku = i.sku WHERE o.customer_id = c.customer_id AND {})"
    )
    references = {
        "same_row": has.format("i.product_type = 'beverage' AND p.category = 'hot'"),
        "separate_rows": has.format("i.product_type = 'beverage'")
        + " AND "
        + has.format("p.category = 'hot'"),
    }
    for option, sql in references.items():
        resent = {**query, "where": options[option]}
        assert _rows(package, resent) == _customers(package, sql), option


def test_one_flat_condition_answers_as_before(package: Path) -> None:
    has_beverage = HAS_ITEM.format("i.product_type = 'beverage'")
    assert _rows(package, _query([BEVERAGE])) == _customers(package, has_beverage) == [(5,)]
    # A group of one condition says the same.
    group = [{"entity": ITEM, "any": [BEVERAGE]}]
    assert _rows(package, _query(group)) == [(5,)]


@pytest.mark.parametrize(
    ("condition", "any_sql", "none_condition", "none_sql", "kept"),
    [
        (
            NOT_BEVERAGE,
            "(i.product_type <> 'beverage' OR i.product_type IS NULL)",
            BEVERAGE,
            "i.product_type = 'beverage'",
            {"any_not": [1, 6], "none": [3, 5]},
        ),
        (
            {"field": PRICE, "op": "missing", "value": True},
            "i.price IS NULL",
            {"field": PRICE, "op": "missing", "value": False},
            "i.price IS NOT NULL",
            {"any_not": [4], "none": [3, 4, 5]},
        ),
    ],
    ids=["not", "missing"],
)
def test_a_negated_flat_condition_asks_which_negation(
    package: Path,
    condition: dict[str, Any],
    any_sql: str,
    none_condition: dict[str, Any],
    none_sql: str,
    kept: dict[str, list[int]],
) -> None:
    """'Has an item that is not a beverage' and 'has no beverage item' differ."""
    query = _query([condition])
    error = _refusal(package, query)
    assert error["code"] == "AMBIGUOUS_CHILD_SCOPE"
    options = _options(error)
    assert options == {"any_not": [_any(condition)], "none": [_none(none_condition)]}
    references = {"any_not": HAS_ITEM.format(any_sql), "none": "NOT " + HAS_ITEM.format(none_sql)}
    for option, sql in references.items():
        assert _rows(package, {**query, "where": options[option]}) == _customers(package, sql)
        assert _who(package, options[option]) == _ids(package, sql) == kept[option]


def _payment(*conditions: dict[str, Any]) -> dict[str, Any]:
    return {"entity": PAYMENT, "any": list(conditions)}


def _session(*conditions: dict[str, Any]) -> dict[str, Any]:
    return {"entity": SESSION, "any": list(conditions)}


# (where, words its why_invalid names). Customer 6's beverage is on order 60 and its card
# payment on order 61: whether an item and a payment must share an order changes the answer
# (customers 1 and 6, or 1 alone), so no question about items alone can settle it.
OTHER_SHAPES = {
    # An item and a payment of a customer's orders: the same order, or any?
    "item_filters_beside_a_payment_filter": ([BEVERAGE, OVER_5, CARD], ""),
    "item_filter_beside_a_payment_filter": ([BEVERAGE, CARD], ""),
    "item_group_beside_a_payment_filter": ([_any(BEVERAGE), CARD], ""),
    "item_group_beside_a_payment_group": ([_any(BEVERAGE), _payment(CARD)], ""),
    # A customer's items and sessions.
    "item_group_beside_a_session_filter": ([_any(BEVERAGE), WEB], ""),
    "item_group_beside_a_session_group": (
        [_any(BEVERAGE), _session(WEB)],
        "one child scope per query",
    ),
    "item_group_beside_a_none_session_group": (
        [_any(BEVERAGE), {"entity": SESSION, "none": [WEB]}],
        "one child scope per query",
    ),
    # A flat filter beside a group on its own child: in the group's row, or another?
    "flat_beside_any": ([BEVERAGE, _any(OVER_5)], ""),
    "flat_beside_none": ([BEVERAGE, _none(OVER_5)], ""),
    "none_beside_flat": ([_none(BEVERAGE), OVER_5], ""),
    "flat_beside_two_groups": ([_any(BEVERAGE), _any(UNDER_10), OVER_5], ""),
    "flat_beside_two_groups_under_5": ([_any(BEVERAGE), _any(OVER_5), UNDER_5], ""),
    # A negated filter beside another filter on the child, or two negated filters.
    "negated_beside_flat": ([NOT_BEVERAGE, OVER_5], ""),
    "negated_beside_any": ([_any(OVER_5), NOT_BEVERAGE], ""),
    "flat_beside_negated": ([BEVERAGE, NOT_17], ""),
    "two_negated": ([NOT_BEVERAGE, NOT_17], ""),
}


def test_every_other_shape_keeps_its_refusal(payments_package: Path) -> None:
    """Only two shapes are asked about: two or more positive flat filters on one child, or one
    negated flat filter (every negation has an exact complement). Beside an entity filter
    nothing else may cross a one-to-many hop, and entity filters stay on one child."""
    for shape, (where, why) in OTHER_SHAPES.items():
        error = _refusal(payments_package, _query(where))
        assert error["code"] == "MIXED_GRAIN_INVALID", shape
        assert "clarification" not in error.get("details", {}), shape
        assert why in error["why_invalid"], shape


def test_an_unknown_operator_is_refused_before_child_scope_analysis(package: Path) -> None:
    for field, value, op in [(TYPE, "beverage", "IS NOT"), (PRICE, 17, "LIKE")]:
        error = _refusal(package, _query([{"field": field, "op": op, "value": value}]))
        assert error["code"] == "INVALID_QUERY"
        assert error["details"]["path"] == "where[0].op"
        assert error["recovery_hints"][0]["code"] == "USE_FILTER_WORD"
        assert "clarification" not in error.get("details", {})


def test_each_reading_answers_for_every_measure_entity(package: Path) -> None:
    """Each leaf reads the offered groups from its own entity: customers, and orders."""
    query = _query([BEVERAGE, OVER_5], "customer_count", "order_count")
    options = _options(_refusal(package, query))
    assert options == {"same_row": SAME_ROW, "separate_rows": SEPARATE_ROWS}
    item = "EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.order_id AND {})"
    references = {
        "same_row": (SAME_ROW_SQL, item.format("i.product_type = 'beverage' AND i.price > 5")),
        "separate_rows": (
            SEPARATE_ROWS_SQL,
            item.format("i.product_type = 'beverage'") + " AND " + item.format("i.price > 5"),
        ),
    }
    for option, (customers, orders) in references.items():
        reference = (
            f"SELECT (SELECT COUNT(*) FROM customers c WHERE {customers}), "
            f"(SELECT COUNT(*) FROM orders o WHERE {orders})"
        )
        resent = {**query, "where": options[option]}
        assert _rows(package, resent) == _reference(package, reference), option


@pytest.mark.parametrize(
    ("where", "option"),
    [([BEVERAGE, OVER_5], "same_row"), ([NOT_BEVERAGE], "any_not")],
    ids=["two_flat", "negated"],
)
def test_a_child_grain_measure_beside_ambiguous_filters_keeps_its_refusal(
    package: Path, where: list[Any], option: str
) -> None:
    """Item revenue's own rows are items: no group on items can filter it, so no reading can be
    offered, and the query keeps the refusal it had before groups existed. The refusal names
    the reading and its code in its message only: it carries no details."""
    error = _refusal(package, _query(where, "customer_count", "item_revenue"))
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert "clarification" not in error.get("details", {})
    assert "child group" in error["why_invalid"]
    assert f"'{option}' reading" in error["message"] and "(INVALID_QUERY)" in error["message"]


def test_a_question_with_a_reading_that_cannot_answer_is_refused(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the bypass: a builder adds a reading no query can answer (a group on the measure's
    own entity). The guard in bind_query binds every option first; one that fails withdraws the
    whole question, never leaving the others offered, and names only its id and code."""
    from semantic_rails.compiler_parts import plan_checks

    built = plan_checks._child_scope_clarification
    broken = {
        "id": "broken",
        "meaning": "",
        "where": [{**_any(BEVERAGE), "entity": "entity.scope_customer"}],
    }

    def clarification(*args: Any) -> SemanticLayerError:
        error = built(*args)
        error.details["clarification"]["options"].append(broken)
        error.details["recovery_hints"].append({"option": "broken"})
        return error

    config = load_package_config(str(package))
    monkeypatch.setattr(plan_checks, "_child_scope_clarification", clarification)
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), _engine(config, _query([BEVERAGE, OVER_5])))
    assert caught.value.code == "MIXED_GRAIN_INVALID"
    assert caught.value.details == {}
    assert "'broken' reading" in str(caught.value) and "(INVALID_QUERY)" in str(caught.value)


# ---- Refusals -------------------------------------------------------------------------

NORTH = {"field": REGION, "op": "is", "value": "North"}
DISTRIBUTION = {
    "expression": {
        "kind": "distribution",
        "function": "avg",
        "over": {
            "kind": "entity_value",
            "entity": "entity.scope_customer",
            "input": "measure.scope.order_count",
        },
    },
    "as": "orders_per_customer",
}
CREDIT_NOT_ON_BEVERAGES = {
    "kind": "aggregate",
    "measure": "measure.scope.credit",
    "aggregation": "sum",
    "filter": {"all": [NOT_BEVERAGE]},
}

# (query, code, details it carries, words its why_invalid names)
REFUSALS = {
    "lookup_only_child": (
        _query([{"entity": "entity.scope_region", "any": [NORTH]}]),
        "INVALID_QUERY",
        {"path": "where[0].entity"},
        "",
    ),
    "own_entity": (
        _query([{"entity": "entity.scope_customer", "any": [NORTH]}]),
        "INVALID_QUERY",
        {"path": "where[0].entity"},
        "",
    ),
    "condition_off_the_child": (
        _query([_any(WEB)]),
        "INVALID_QUERY",
        {"path": "where[0].entity"},
        "",
    ),
    "nested_group": (
        _query([_any(_any(BEVERAGE))]),
        "INVALID_QUERY",
        {"path": "where[0].any[0]"},
        "",
    ),
    "unknown_match": (
        _query([{"entity": ITEM, "all": [BEVERAGE]}]),
        "INVALID_QUERY",
        {"path": "where[0].all"},
        "",
    ),
    "missing_match": (_query([{"entity": ITEM}]), "INVALID_QUERY", {"path": "where[0]"}, ""),
    "empty_group": (_query([_any()]), "INVALID_QUERY", {"path": "where[0].any"}, ""),
    "group_and_filter_keys": (
        _query([{**_any(BEVERAGE), "field": TYPE}]),
        "INVALID_QUERY",
        {"path": "where[0].field"},
        "",
    ),
    # For an order measure, a lookup from the item to its order would bind the orders table
    # inside EXISTS to the lookup's row and part it from the order being counted.
    "group_reads_the_parent_back": (
        _query(
            [
                _any(
                    BEVERAGE, {"field": "dimension.scope_order_customer_id", "op": "is", "value": 1}
                )
            ],
            "order_count",
        ),
        "INVALID_QUERY",
        {"tables": ["orders"]},
        "",
    ),
    "unknown_child": (
        _query([{"entity": "entity.scope_nothing", "any": [BEVERAGE]}]),
        "OBJECT_NOT_FOUND",
        {},
        "",
    ),
    "unknown_dimension": (
        _query([_any({"field": "dimension.nope", "op": "is", "value": 1})]),
        "OBJECT_NOT_FOUND",
        {},
        "",
    ),
    "wrong_value_type": (_query([_any({**OVER_5, "value": "five"})]), "INVALID_QUERY", {}, ""),
    # Item revenue is at the child's grain: a group of its own entity is a plain where.
    "child_grain_measure_beside_a_group": (
        _query(SAME_ROW, "customer_count", "item_revenue"),
        "INVALID_QUERY",
        {},
        "",
    ),
    "grouped_by_a_child_dimension": (
        {**_query(SEPARATE_ROWS), "group_by": [TYPE]},
        "MIXED_GRAIN_INVALID",
        {},
        "child group",
    ),
    "no_measure": ({"group_by": [REGION], "where": SAME_ROW}, "INVALID_QUERY", {}, ""),
    # Scopes inside a measure's own filter are not offered.
    "measure_filter": (
        {"select": [{"expression": CREDIT_NOT_ON_BEVERAGES, "as": "credit"}]},
        "MIXED_GRAIN_INVALID",
        {},
        "",
    ),
    # Per-entity values across the group's hop are refused, as under a flat child filter.
    "distribution_beside_a_group": (
        {"select": [DISTRIBUTION], "where": SAME_ROW},
        "MIXED_GRAIN_INVALID",
        {},
        "distribution",
    ),
}


def test_every_malformed_or_unsupported_group_is_refused(package: Path) -> None:
    for case, (query, code, details, why) in REFUSALS.items():
        error = _refusal(package, query)
        assert error["code"] == code, case
        assert details.items() <= error.get("details", {}).items(), case
        assert "clarification" not in error.get("details", {}), case
        assert why in error.get("why_invalid", ""), case
    assert _run(package, {"select": [DISTRIBUTION]}, validate=True)["ok"] is True


def test_a_segment_membership_refuses_a_group(package: Path) -> None:
    from semantic_rails.schema import SegmentConfig
    from semantic_rails.segments import normalize_segment

    config = load_package_config(str(package))
    segment = SegmentConfig(
        id="segment.scope.beverage_buyers",
        entity="entity.scope_customer",
        basis_metric="metric.scope.customers",
        # A package's membership filter, in the compiler's own form.
        where=[{"child": ITEM, "match": "any", "where": [BEVERAGE, OVER_5]}],
    )
    config = replace(config, segments=[segment])
    with pytest.raises(SemanticLayerError) as caught:
        normalize_segment(config, segment.id)
    assert caught.value.code == "INVALID_SEGMENT"


# ---- Guards ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("where", "dimensions"),
    [
        (SAME_ROW, (TYPE, PRICE)),
        (SAME_ROW, (TYPE,)),
        (
            [
                {
                    "entity": SESSION,
                    "any": [{"field": CUSTOMER_ID, "op": "is", "value": 7}],
                }
            ],
            (CUSTOMER_ID,),
        ),
        (
            [
                {
                    "entity": SESSION,
                    "none": [{"field": CUSTOMER_ID, "op": "is", "value": 7}],
                }
            ],
            (CUSTOMER_ID,),
        ),
    ],
    ids=["granted_child_dimensions", "ungranted_dimension", "parent_only_any", "parent_only_none"],
)
def test_restricted_grants_cannot_authorize_a_child_scope_with_dimensions(
    package: Path, monkeypatch: pytest.MonkeyPatch, where: list[Any], dimensions: tuple[str, ...]
) -> None:
    context = RequestContext(
        actor="subject",
        metric_allowlist=("metric.scope.customers",),
        dimension_allowlist=dimensions,
    )
    query = {
        "select": [{"expression": "metric.scope.customers", "as": "value"}],
        "where": where,
    }
    engine = Runtime.from_path(str(package))
    try:
        # Warm the unrestricted path, then ensure denial precedes warehouse access.
        assert engine.query(query)["ok"] is True
        assert engine.validate({**query, "where": []}, context=context)["ok"] is True

        def no_warehouse(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("A restricted child scope reached the warehouse")

        monkeypatch.setattr("semantic_rails.runtime._adapter_query", no_warehouse)
        denied = engine.validate(query, context=context)
        assert denied["ok"] is False
        assert denied["errors"][0]["code"] == "RESOURCE_ACCESS_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as caught:
                operation(query, context=context)
            assert caught.value.code == "RESOURCE_ACCESS_DENIED"
    finally:
        engine.close()


def test_a_cut_policy_sees_the_conditions_inside_a_group(package: Path) -> None:
    config = load_package_config(str(package))
    policy = SemanticPolicyConfig(
        id="policy.scope.type_cuts",
        kind="metric_constraint",
        object_ids=["measure.scope.customer_count"],
        roles=["analyst"],
        config={"allowed_where": [TYPE]},
    )
    config = replace(config, semantic_policies=[policy])
    engine = Runtime.from_config(config, source_path=str(package))
    context = {"roles": ["analyst"]}
    try:
        allowed = engine.validate(_query([_any(BEVERAGE)]), context=context)
        denied = engine.validate(_query(SAME_ROW), context=context)
    finally:
        engine.close()
    assert allowed["ok"] is True, allowed
    assert denied["ok"] is False
    violation = denied["policy_effects"][0]["violations"][0]
    assert violation == {"kind": "disallowed_where", "disallowed": [PRICE], "allowed": [TYPE]}


def test_a_denied_dimension_inside_a_group_is_denied(package: Path) -> None:
    config = load_package_config(str(package))
    policy = SemanticPolicyConfig(
        id="policy.scope.hide_price",
        kind="object_access",
        object_ids=[PRICE],
        audiences=["partner"],
        action="deny",
    )
    config = replace(config, semantic_policies=[policy])
    engine = Runtime.from_config(config, source_path=str(package))
    context = {"audience": "partner"}
    try:
        allowed = engine.validate(_query([_any(BEVERAGE)]), context=context)
        denied = engine.validate(_query(NO_SAME_ROW), context=context)
    finally:
        engine.close()
    assert allowed["ok"] is True, allowed
    assert denied["ok"] is False
    assert denied["errors"][0]["code"] == "POLICY_DENIED"


@pytest.mark.parametrize(
    "policy",
    [
        SemanticPolicyConfig(
            id="policy.scope.require_beverage",
            kind="metric_constraint",
            object_ids=["measure.scope.customer_count"],
            roles=["analyst"],
            config={"required_where": [BEVERAGE]},
        ),
        SemanticPolicyConfig(
            id="policy.scope.type_cuts",
            kind="metric_constraint",
            object_ids=["measure.scope.customer_count"],
            roles=["analyst"],
            config={"allowed_where": [TYPE]},
        ),
        SemanticPolicyConfig(
            id="policy.scope.hide_price",
            kind="object_access",
            object_ids=[PRICE],
            roles=["analyst"],
            action="deny",
        ),
    ],
    ids=["required_where", "allowed_where", "denied_dimension"],
)
def test_no_reading_is_offered_that_the_callers_policies_deny(
    package: Path, policy: SemanticPolicyConfig
) -> None:
    """Each reading moves both filters into groups: a required filter is then no plain filter,
    a disallowed dimension is still read, and so is a denied one. Every reading would be
    POLICY_DENIED for an analyst, so none is offered; others are still asked."""
    config = replace(load_package_config(str(package)), semantic_policies=[policy])
    engine = Runtime.from_config(config, source_path=str(package))
    query = _query([BEVERAGE, OVER_5])
    context = {"roles": ["analyst"]}
    try:
        report = engine.validate(query, context=context)
        errors = [report["errors"][0]]
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as caught:
                operation(query, context=context)
            errors.append({"code": caught.value.code, "details": caught.value.details})
        asked = engine.validate(_query([BEVERAGE, OVER_5]))
    finally:
        engine.close()
    assert [error["code"] for error in errors] == ["MIXED_GRAIN_INVALID"] * 3
    assert all("clarification" not in error.get("details", {}) for error in errors)
    assert "'same_row' reading" in errors[0]["message"]
    assert "(POLICY_DENIED)" in errors[0]["message"]
    assert asked["errors"][0]["code"] == "AMBIGUOUS_CHILD_SCOPE"


def test_a_rollup_holding_the_group_dimension_is_not_used(package: Path) -> None:
    config = load_package_config(str(package))
    rollup = AggregateRelationConfig(
        id="aggregate_relation.customers_by_type",
        relation="customers_by_type",
        source_entity="entity.scope_customer",
        dimensions=[TYPE, PRICE],
    )
    config = replace(config, aggregate_relations=[rollup])
    compiled = compile_query(config, Registry(config), _engine(config, _query(SAME_ROW)))
    [leaf] = compiled["logical_plan"].measure_plans
    assert leaf.aggregate_relation_id == ""
    assert leaf.aggregate_relation_rejections == {rollup.id: "child_group"}
    assert "customers_by_type" not in compiled["sql"]


def test_a_leaf_planned_without_the_group_route_is_refused_not_answered(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the bypass: plan the leaf as if the group were not there. The plan's guard refuses;
    with that guard gone too, the ordinary leaf refuses the group rather than drop it."""
    from semantic_rails.compiler_parts import planning

    planned = planning._leaf_path_selections

    def without_groups(bound: Any, config: Any, query: Any) -> Any:
        selections, entities, _ = planned(bound, config, replace(query, where=[]))
        return selections, entities, ""

    config = load_package_config(str(package))
    monkeypatch.setattr(planning, "_leaf_path_selections", without_groups)
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), _engine(config, _query(SAME_ROW)))
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.details["path"] == "where"
    assert caught.value.details["measure"] == "measure.scope.customer_count"

    monkeypatch.setattr(planning, "_require_child_group_leaves", lambda *args: None)
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(config, Registry(config), _engine(config, _query(SAME_ROW)))
    assert caught.value.code == "INVALID_QUERY"
    assert (caught.value.details["path"], caught.value.details["child"]) == ("where[0]", ITEM)


# ---- Policies and dialects ------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "attributes"),
    [
        ({"dimension": TYPE, "attribute": "type", "type": "string"}, {"type": "beverage"}),
        ({"dimension": CUSTOMER_ID, "attribute": "customer", "type": "integer"}, {"customer": 1}),
    ],
    ids=["child_policy", "parent_policy"],
)
@pytest.mark.parametrize("where", [SAME_ROW, NO_SAME_ROW], ids=["any", "none"])
def test_groups_are_denied_under_a_row_policy(
    package: Path, policy: dict[str, Any], attributes: dict[str, Any], where: list[Any]
) -> None:
    config = load_package_config(str(package))
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(id="policy.scope.rows", kind="row_filter", config=policy)
        ],
    )
    context = RequestContext(attributes=attributes).to_policy_context()
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(
            config,
            Registry(config),
            _engine(config, _query(where)),
            row_filters=row_filters_for_context(config, context),
        )
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details["reason"] == "row_filter_unsupported_query"


@pytest.mark.parametrize("where", [[BEVERAGE, OVER_5], [NOT_BEVERAGE]], ids=["two", "negated"])
def test_no_reading_is_offered_under_a_row_policy_that_denies_groups(
    package: Path, where: list[Any]
) -> None:
    """Every reading is a group, and the row policy denies groups: the query keeps the
    refusal it had before groups existed instead of asking a question with no answer."""
    config = load_package_config(str(package))
    policy = {"dimension": CUSTOMER_ID, "attribute": "customer", "type": "integer"}
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(id="policy.scope.rows", kind="row_filter", config=policy)
        ],
    )
    context = RequestContext(attributes={"customer": 1}).to_policy_context()
    for row_filters, code in [
        ((), "AMBIGUOUS_CHILD_SCOPE"),
        (row_filters_for_context(config, context), "MIXED_GRAIN_INVALID"),
    ]:
        with pytest.raises(SemanticLayerError) as caught:
            compile_query(
                config, Registry(config), _engine(config, _query(where)), row_filters=row_filters
            )
        assert caught.value.code == code
        assert ("clarification" in caught.value.details) is (code == "AMBIGUOUS_CHILD_SCOPE")


CLICKHOUSE_SQL = """WITH leaf_1__leaf_1_entity_rows AS (
SELECT DISTINCT
  customers.customer_id AS __entity_key_1,
  customers.customer_id AS __entity_value
FROM customers
INNER JOIN orders ON customers.customer_id = orders.customer_id
INNER JOIN items ON orders.order_id = items.order_id
WHERE
  items.product_type = 'beverage'
  AND items.price > 5
),
leaf_1 AS (
SELECT
  COUNT(DISTINCT leaf_1__leaf_1_entity_rows.__entity_value) AS m1
FROM leaf_1__leaf_1_entity_rows
),
guarded_base AS (
SELECT
  CASE WHEN MAX(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1
FROM leaf_1 AS base
)
SELECT
  base.m1 AS "customer_count"
FROM guarded_base AS base
SETTINGS join_use_nulls = 1"""


def _clickhouse(package: Path) -> Any:
    config = load_package_config(str(package))
    return replace(config, package=replace(config.package, warehouse="clickhouse"))


def test_clickhouse_answers_any_with_its_distinct_parent_leaf(package: Path) -> None:
    config = _clickhouse(package)
    query = {**_query(SAME_ROW), "observation_scope": "query"}  # the leaf, without a probe
    sql = compile_query(config, Registry(config), _engine(config, query))["sql"]
    assert sql == CLICKHOUSE_SQL
    duckdb_sql = sql.removesuffix("\nSETTINGS join_use_nulls = 1")
    assert _reference(package, duckdb_sql) == _customers(package, SAME_ROW_SQL) == [(2,)]


def test_clickhouse_refuses_groups_its_leaf_cannot_answer_and_asks_nothing(package: Path) -> None:
    """ClickHouse's leaf answers one 'any' group on the child's own columns: two groups, a
    'none' group and a group through a lookup are refused there, and so no question is asked
    whose reading would be one of them. Only the message names the refused reading."""
    config = _clickhouse(package)
    hot = {"field": CATEGORY, "op": "is", "value": "hot"}
    for where, option in [
        (NO_SAME_ROW, ""),
        (SEPARATE_ROWS, ""),
        ([_any(hot)], ""),
        ([BEVERAGE, OVER_5], "separate_rows"),
        ([NOT_BEVERAGE], "none"),
        ([BEVERAGE, hot], "same_row"),
    ]:
        with pytest.raises(SemanticLayerError) as caught:
            compile_query(config, Registry(config), _engine(config, _query(where)))
        assert caught.value.code == "MIXED_GRAIN_INVALID", where
        if option:
            assert "clarification" not in caught.value.details
            assert f"'{option}' reading" in str(caught.value)
        else:
            assert "ClickHouse" in caught.value.details["why_invalid"]


def test_other_dialects_render_each_group_as_correlated_exists(package: Path) -> None:
    config = load_package_config(str(package))
    for warehouse in ("postgres", "snowflake", "bigquery", "databricks"):
        dialect_config = replace(config, package=replace(config.package, warehouse=warehouse))
        query = _engine(dialect_config, _query([*SAME_ROW, *NO_SAME_ROW]))
        sql = compile_query(dialect_config, Registry(dialect_config), query)["sql"]
        assert sql.count("NOT EXISTS (") == 1
        assert sql.count("EXISTS (") == 4  # two nested hops per group


# ---- Routes ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision_scope", ["package", "query"])
def test_an_ambiguous_child_route_is_never_answered(package: Path, decision_scope: str) -> None:
    """Items reach orders through two relationships of one length: which items are meant?"""
    config = load_package_config(str(package))
    [items_order] = [rel for rel in config.relationships if rel.id == "relationship.items_order"]
    returned = replace(
        items_order,
        id="relationship.items_return_order",
        source_column="order_id",
        source_columns=["order_id"],
    )
    tied = replace(config, relationships=[*config.relationships, returned])
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(tied, Registry(tied), _engine(tied, _query(SAME_ROW)))
    assert caught.value.code == "AMBIGUOUS_PATH"
    assert len(caught.value.details["clarification"]["options"]) == 2
    decision = route_pin(
        "entity.scope_customer",
        ITEM,
        ["relationship.orders_customer", "relationship.items_order"],
    )
    pinned = (
        replace(tied, path_preferences=[PathPreferenceConfig(**decision)])
        if decision_scope == "package"
        else tied
    )
    route = {
        "from": decision["source_entity"],
        "to": decision["target_entity"],
        "via": decision["relationship_path"],
    }
    extra = {"routes": [route]} if decision_scope == "query" else {}
    compiled = compile_query(pinned, Registry(pinned), _engine(pinned, _query(SAME_ROW, **extra)))
    assert _reference(package, compiled["sql"]) == [(2,)]

    # The route decision also survives the guard that binds both child-scope readings.
    query = _query([BEVERAGE, OVER_5], **extra)
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(pinned, Registry(pinned), _engine(pinned, query))
    assert caught.value.code == "AMBIGUOUS_CHILD_SCOPE"
    options = _options({"details": caught.value.details})
    for option, reference in [("same_row", SAME_ROW_SQL), ("separate_rows", SEPARATE_ROWS_SQL)]:
        resent = _engine(pinned, {**query, "where": options[option]})
        compiled = compile_query(pinned, Registry(pinned), resent)
        assert _reference(package, compiled["sql"]) == _customers(package, reference), option


def test_a_lookup_first_route_beside_another_candidate_is_ambiguous(package: Path) -> None:
    """An order reaches sessions through its customer, or through its customer's region if
    sessions carried one. A shorter route doesn't say which sessions the question means."""
    config = load_package_config(str(package))
    [customer_region] = [
        rel for rel in config.relationships if rel.id == "relationship.customers_region"
    ]
    session_region = replace(
        customer_region, id="relationship.sessions_region", source_entity=SESSION
    )
    longer = replace(config, relationships=[*config.relationships, session_region])
    where = [{"entity": SESSION, "any": [WEB]}]
    with pytest.raises(SemanticLayerError) as caught:
        compile_query(longer, Registry(longer), _engine(longer, _query(where, "order_count")))
    assert caught.value.code == "AMBIGUOUS_PATH"
    assert len(caught.value.details["clarification"]["options"]) == 2
    pinned = replace(
        longer,
        path_preferences=[
            PathPreferenceConfig(
                source_entity="entity.scope_order",
                target_entity=SESSION,
                relationship_path=[
                    "relationship.orders_customer",
                    "relationship.sessions_customer",
                ],
            )
        ],
    )
    compiled = compile_query(
        pinned, Registry(pinned), _engine(pinned, _query(where, "order_count"))
    )
    reference = (
        "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM customers c "
        "JOIN sessions s ON s.customer_id = c.customer_id "
        "WHERE c.customer_id = o.customer_id AND s.channel = 'web')"
    )
    assert _reference(package, compiled["sql"]) == _reference(package, reference)


def test_each_reading_keeps_the_route_its_filters_take(item_customer_package: Path) -> None:
    """The package records a customer's products through its orders. Filters on a product's
    category follow that route to items; a group on items must too. While a customer's items
    are unrecorded, no reading is offered: the refusal names the row that would record the
    filters' route. A row recording the items that name the customer (all customer 3's)
    disagrees with the products row, so that package does not load."""
    package = item_customer_package
    config = load_package_config(str(package))
    customer, product = "entity.scope_customer", "entity.scope_product"
    through_orders = ["relationship.orders_customer", "relationship.items_order"]
    [direct] = [
        rel.id
        for rel in config.relationships
        if {rel.source_entity, rel.target_entity} == {ITEM, customer}
    ]
    product_pin = PathPreferenceConfig(
        source_entity=customer,
        target_entity=product,
        relationship_path=[*through_orders, "relationship.items_product"],
    )
    hot = {"field": CATEGORY, "op": "is", "value": "hot"}
    warm = {"field": CATEGORY, "op": "is", "value": ["hot", "soda"]}
    has = (
        "EXISTS (SELECT 1 FROM orders o JOIN items i ON i.order_id = o.order_id "
        "LEFT JOIN products p ON p.sku = i.sku WHERE o.customer_id = c.customer_id AND {})"
    )

    def answer(pins: list[PathPreferenceConfig], where: list[Any]) -> list[tuple[Any, ...]]:
        pinned = replace(config, path_preferences=pins)
        engine = Runtime.from_config(pinned, source_path=str(package))
        try:
            result = engine.query(_query(where))
        finally:
            engine.close()
        return _normal(tuple(row.values()) for row in typed_rows(result))

    # One filter follows the recorded route: customers 4 and 7 ordered tea.
    assert answer([product_pin], [hot]) == _customers(package, has.format("p.category = 'hot'"))
    assert answer([product_pin], [hot]) == [(2,)]
    pin_row = json.dumps(route_pin(customer, ITEM, through_orders))
    with pytest.raises(SemanticLayerError) as caught:
        answer([product_pin], [hot, warm])
    assert caught.value.code == "MIXED_GRAIN_INVALID"
    assert "clarification" not in caught.value.details
    assert pin_row in caught.value.details["why_invalid"]
    direct_pin = PathPreferenceConfig(customer, ITEM, [direct])
    with pytest.raises(SemanticLayerError) as caught:
        answer([product_pin, direct_pin], [hot, warm])
    assert caught.value.code == "INVALID_CONFIG"
    pins = [product_pin, PathPreferenceConfig(customer, ITEM, through_orders)]
    with pytest.raises(SemanticLayerError) as caught:
        answer(pins, [hot, warm])
    assert caught.value.code == "AMBIGUOUS_CHILD_SCOPE"
    options = {row["id"]: row["where"] for row in caught.value.details["clarification"]["options"]}
    references = {
        "same_row": has.format("p.category = 'hot' AND p.category IN ('hot', 'soda')"),
        "separate_rows": has.format("p.category = 'hot'")
        + " AND "
        + has.format("p.category IN ('hot', 'soda')"),
    }
    assert set(options) == set(references)
    for option, where in options.items():
        assert answer(pins, where) == _customers(package, references[option]) == [(2,)], option
    # A group on the other recorded route reads the items that name customer 3.
    assert answer([direct_pin], [_any(hot)]) == [(1,)]


# ---- Agreement ------------------------------------------------------------------------

SHAPES = {
    "same_row": _query(SAME_ROW),
    "parent_sum": _query(SEPARATE_ROWS, "credit"),
    "lookup_group": {**_query(SEPARATE_ROWS), "group_by": [REGION]},
    "two_children": _query([SEPARATE_ROWS[0], {"entity": SESSION, "none": [WEB]}]),
    "two_flat": _query([BEVERAGE, OVER_5]),
    "negated_flat": _query([NOT_BEVERAGE]),
    "one_flat": _query([BEVERAGE]),
    "lookup_only_child": _query(
        [
            {
                "entity": "entity.scope_region",
                "any": [{"field": REGION, "op": "is", "value": "North"}],
            }
        ]
    ),
    "child_grouping": {**_query(SEPARATE_ROWS), "group_by": [TYPE]},
}


@pytest.mark.parametrize("shape", SHAPES)
def test_validate_compile_execute_and_plan_agree(package: Path, shape: str) -> None:
    query = SHAPES[shape]
    engine = Runtime.from_path(str(package))
    mcp = SemanticLayerMCPAdapter(engine)
    try:
        report = engine.validate(query)
        outcomes = []
        for call in (engine.compile, engine.query):
            try:
                result = call(query)
                outcomes.append(("ok", result.get("rendered_sql") or result.get("sql")))
            except SemanticLayerError as exc:
                outcomes.append((exc.code, None))
        tool = [
            mcp.call_tool("execute", {"query": query, "mode": mode})
            for mode in ("validate", "sql", "run")
        ]
        planned = plan_payload(engine, intent="customers", partial_query=query)
    finally:
        mcp.close()
    code = "ok" if report["ok"] else report["errors"][0]["code"]
    assert [outcome[0] for outcome in outcomes] == [code, code]
    assert [("ok" if row["ok"] else row["error"]["code"]) for row in tool] == [code] * 3
    if code == "ok":
        assert outcomes[0][1] and outcomes[0][1] == outcomes[1][1]
    # plan carries the caller's where unchanged and validates it the same way.
    best = planned["best"]
    assert best["query"]["where"] == query["where"]
    assert best["validation_ok"] is (code == "ok")
    why = planned.get("why") or {}
    if code == "AMBIGUOUS_CHILD_SCOPE":
        # plan asks the engine's question, each reading a whole call that runs.
        assert planned["status"] == "needs_clarification"
        assert why["code"] == code
        for option in why["details"]["options"]:
            assert _run(package, option["call"], validate=True)["ok"], option
    else:
        plan_codes = [error["code"] for error in why.get("errors", [])]
        assert plan_codes == ([] if code == "ok" else [code])
