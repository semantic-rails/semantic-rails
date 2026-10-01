"""Lookups keep the rows they find no match for, in every query shape.

An N:1 or 1:1 hop never removes a measure's row, whatever reads the looked-up field (a
grouping, a filter, the measure's own filter, an aggregate_if or a measure expression) and in
every leaf (the plain leaf, the pre-aggregation shortcut, the de-duplicated leaf, the
entity_in_terms_of leaf, the EXISTS leaf and the snapshot of an entity-set ratio). A row whose
foreign key is NULL or matches no row stays, with NULL for everything the hop looks up: it
groups under NULL, so grouped rows add up to the ungrouped total, `IS NULL` selects it, and
`=`, `!=`, `IN` and `NOT IN` never match it, wherever the filter is written.

Fixture: orders -> customer -> region -> country, three lookup hops. At each hop one row has a
NULL foreign key, one a foreign key with no record, and one a match whose attribute is NULL:
order 10 has no customer, order 11 a customer with no record (C9) and C8 no segment; C6 has no
region, C7 a region with no record (R9) and R5 no name; R3 has no country, R4 a country with no
record (K9) and K2 no name. Orders hold items (one-to-many), each a lookup of a product: item
104 has no product, item 105 one with no record and melt no category. Gold values come from
SQL written independently of the engine: scalar subqueries read NULL where a lookup finds no
row, with no joins.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlJoin

SEED_SQL = """
CREATE TABLE countries (country_id VARCHAR, country_name VARCHAR);
INSERT INTO countries VALUES ('K1', 'Atlantis'), ('K2', NULL);
CREATE TABLE regions (region_id VARCHAR, country_id VARCHAR, region_name VARCHAR);
INSERT INTO regions VALUES ('R1', 'K1', 'North'), ('R2', 'K2', 'South'), ('R3', NULL, 'East'),
  ('R4', 'K9', 'West'), ('R5', 'K1', NULL);
CREATE TABLE customers (customer_id VARCHAR, region_id VARCHAR, segment VARCHAR);
INSERT INTO customers VALUES ('C1', 'R1', 'retail'), ('C2', 'R2', 'retail'),
  ('C3', 'R3', 'wholesale'), ('C4', 'R4', 'retail'), ('C5', 'R5', 'wholesale'),
  ('C6', NULL, 'retail'), ('C7', 'R9', 'wholesale'), ('C8', 'R1', NULL);
CREATE TABLE orders (order_id INTEGER, customer_id VARCHAR, ordered_at TIMESTAMP,
  amount INTEGER);
INSERT INTO orders VALUES
  (1, 'C1', TIMESTAMP '2026-01-05 10:00:00', 10), (2, 'C1', TIMESTAMP '2026-01-06 10:00:00', 20),
  (3, 'C2', TIMESTAMP '2026-01-07 10:00:00', 5), (4, 'C3', TIMESTAMP '2026-01-08 10:00:00', 7),
  (5, 'C4', TIMESTAMP '2026-01-09 10:00:00', 11), (6, 'C5', TIMESTAMP '2026-01-10 10:00:00', 13),
  (7, 'C6', TIMESTAMP '2026-01-11 10:00:00', 17), (8, 'C7', TIMESTAMP '2026-01-12 10:00:00', 19),
  (9, 'C8', TIMESTAMP '2026-01-13 10:00:00', 23), (10, NULL, TIMESTAMP '2026-01-14 10:00:00', 29),
  (11, 'C9', TIMESTAMP '2026-01-15 10:00:00', 31);
CREATE TABLE products (sku VARCHAR, category VARCHAR);
INSERT INTO products VALUES ('coffee', 'hot'), ('tea', 'hot'), ('toast', 'cold'), ('melt', NULL);
CREATE TABLE items (item_id INTEGER, order_id INTEGER, sku VARCHAR, item_type VARCHAR);
INSERT INTO items VALUES (101, 1, 'coffee', 'beverage'), (102, 1, 'tea', 'beverage'),
  (103, 2, 'toast', 'food'), (104, 3, NULL, 'beverage'), (105, 4, 'cake', 'food'),
  (106, 5, 'melt', 'food'), (107, 6, 'coffee', 'beverage'), (108, 10, 'tea', 'beverage'),
  (109, 11, 'toast', 'food'), (110, 9, 'tea', 'beverage');
"""

PACKAGE = """
schema_version: 1
package:
  id: geo
  namespace: geo
  warehouse: duckdb
  default_db: data/geo.duckdb
  seed: {kind: sql_script, source: data/seed.sql}
defaults:
  dimension: {groupable: true, filterable: true}
"""

GRAPH = """
graph:
  entities:
    country: {label: Country, key: [country_id], model: countries}
    region: {label: Region, key: [region_id], model: regions}
    customer: {label: Customer, key: [customer_id], model: customers}
    order: {label: Order, key: [order_id], model: orders}
    item: {label: Item, key: [item_id], model: items}
    product: {label: Product, key: [sku], model: products}
"""

# Lets a distinct order count grouped by an item dimension count from the items
# (entity_in_terms_of); without it, the leaf de-duplicates the orders it joins.
ROLLUP_SAFE_ITEMS = """
  relationships:
    items_order:
      id: relationship.items_order
      entities: [item, order]
      cardinality: many_to_one
      rollup_safe:
        reverse: [count_distinct]
"""

MODELS = {
    "countries": """
        model:
          id: countries
          relation: countries
          entities: {country: {}}
          dimensions:
            country_name: {label: Country, kind: categorical}
        """,
    "regions": """
        model:
          id: regions
          relation: regions
          entities: {region: {}, country: {}}
          dimensions:
            region_name: {label: Region, kind: categorical}
        """,
    "customers": """
        model:
          id: customers
          relation: customers
          entities: {customer: {}, region: {}}
          dimensions:
            segment: {label: Segment, kind: categorical}
        """,
    "orders": """
        model:
          id: orders
          relation: orders
          entities: {order: {}, customer: {}}
          times:
            ordered_at: {label: Ordered at, column: ordered_at, kind: timestamp,
              class: event_time, supported_grains: [day, month], default: true}
          measures:
            amount: {label: Amount, kind: aggregate, expr: amount, default_agg: sum,
              accumulation: {kind: flow}}
            order_count: {label: Orders, kind: entity_count, entity_key: order_id,
              accumulation: {kind: event}}
            order_population: {label: Order population, kind: entity_count,
              entity_key: order_id, accumulation: {kind: population}}
        """,
    "items": """
        model:
          id: items
          relation: items
          entities: {item: {}, order: {}, product: {expr: sku}}
          dimensions:
            item_type: {label: Item type, kind: categorical}
        """,
    "products": """
        model:
          id: products
          relation: products
          entities: {product: {}}
          dimensions:
            category: {label: Category, kind: categorical}
        """,
}

SEGMENT = "dimension.geo_customer_segment"
REGION = "dimension.geo_region_region_name"
COUNTRY = "dimension.geo_country_country_name"
TYPE = "dimension.geo_item_item_type"
CATEGORY = "dimension.geo_product_category"
MONTHLY = {"temporal_role": "temporal_role.geo_order_ordered_at", "grain": "month"}
MONTH = "temporal_role.geo_order_ordered_at__month"
LOOKUP_TABLES = ("customers", "regions", "countries", "products")

# Scalar subqueries read NULL where a lookup finds no row, independently of any join.
SQL_SEGMENT = "(SELECT c.segment FROM customers AS c WHERE c.customer_id = o.customer_id)"
SQL_REGION = (
    "(SELECT r.region_name FROM customers AS c, regions AS r"
    " WHERE c.customer_id = o.customer_id AND r.region_id = c.region_id)"
)
SQL_COUNTRY = (
    "(SELECT k.country_name FROM customers AS c, regions AS r, countries AS k"
    " WHERE c.customer_id = o.customer_id AND r.region_id = c.region_id"
    " AND k.country_id = r.country_id)"
)
GOLD_FIELD = {SEGMENT: SQL_SEGMENT, REGION: SQL_REGION, COUNTRY: SQL_COUNTRY}
# Each dimension, with the lookup tables the query joins to read it.
HOPS = {
    SEGMENT: ["customers"],
    REGION: ["customers", "regions"],
    COUNTRY: ["customers", "regions", "countries"],
}


def _column(entity: str, column: str) -> dict[str, Any]:
    return {"kind": "column", "entity": f"entity.geo_{entity}", "column": column}


def _comparison(left: dict[str, Any], op: str, value: Any) -> dict[str, Any]:
    return {
        "kind": "comparison",
        "op": op,
        "left": left,
        "right": {"kind": "literal", "value": value},
    }


def _aggregate_if(condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": condition,
        "value": _column("order", "order_id"),
    }


# (expression, gold aggregate over orders AS o). The count's condition reads the order alone,
# so a grouping two hops away takes the pre-aggregation shortcut, as the sum does.
AGGREGATIONS = {
    "sum": ({"measure": "measure.geo.amount"}, "SUM(o.amount)"),
    "count": (_aggregate_if(_comparison(_column("order", "amount"), ">", 0)), "COUNT(o.order_id)"),
    "count_distinct": ({"measure": "measure.geo.order_count"}, "COUNT(DISTINCT o.order_id)"),
    "avg": (
        {"kind": "aggregate", "measure": "measure.geo.amount", "aggregation": "avg"},
        "AVG(o.amount)",
    ),
}

# (name, filter clause, gold condition): each holds for rows of every filtered form.
CONDITIONS = {
    "no_region": ({"field": REGION, "op": "IS NULL"}, f"{SQL_REGION} IS NULL"),
    "north": ({"field": REGION, "op": "=", "value": "North"}, f"{SQL_REGION} = 'North'"),
    "no_country": ({"field": COUNTRY, "op": "IS NULL"}, f"{SQL_COUNTRY} IS NULL"),
}


def _write_package(root: Path, *, rollup_safe: bool = False) -> Path:
    pkg = root / "geo"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "segments").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)
    (pkg / "package.yml").write_text(PACKAGE)
    (pkg / "graph.yml").write_text(GRAPH + (ROLLUP_SAFE_ITEMS if rollup_safe else ""))
    for name, body in MODELS.items():
        (pkg / "models" / f"{name}.yml").write_text(textwrap.dedent(body))
    recipe = {"as": "metric.geo.order_amount", "kind": "derived"}
    recipe["expression"] = {"measure": "measure.geo.amount"}
    (pkg / "metrics.yml").write_text(yaml.safe_dump({"metrics": {"geo.order_amount": recipe}}))
    segments = {
        name: {
            "id": f"segment.geo.{name}",
            "label": name,
            "entity": "entity.geo_order",
            "basis_metric": "metric.geo.order_amount",
            "membership": {"where": [clause]},
        }
        for name, (clause, _) in CONDITIONS.items()
    }
    (pkg / "segments" / "core.yml").write_text(yaml.safe_dump({"segments": segments}))
    return pkg


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_package(tmp_path_factory.mktemp("lookups"))


@pytest.fixture(scope="module")
def runtime(package: Path) -> Iterator[Runtime]:
    runtime = Runtime.from_path(str(package))
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def rollup_safe_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    runtime = Runtime.from_path(
        str(_write_package(tmp_path_factory.mktemp("rollup_safe"), rollup_safe=True))
    )
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def gold() -> Iterator[Any]:
    connection = duckdb.connect()
    connection.execute(SEED_SQL)

    def run(sql: str) -> dict[Any, float | None]:
        rows = connection.execute(sql).fetchall()
        return {
            row[:-1] if len(row) > 2 else row[0] if len(row) > 1 else None: _number(row[-1])
            for row in rows
        }

    yield run
    connection.close()


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _query(expression: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"version": 1, "select": [{"as": "value", "expression": expression}], **extra}


def _rows(runtime: Runtime, query: dict[str, Any]) -> dict[Any, float | None]:
    """Values keyed by the query's group (a tuple for several), without the time bucket."""
    keys = list(query.get("group_by", []))
    out: dict[Any, float | None] = {}
    for row in runtime.query(query)["rows"]:
        group = tuple(row[key] for key in keys)
        out[group if len(group) > 1 else group[0] if group else None] = _number(row["value"])
    return out


def _sql(config: Any, query: dict[str, Any]) -> str:
    return " ".join(compile_query(config, Registry(config), query)["sql"].split())


def _lookup_joins(sql: str) -> list[tuple[str, str]]:
    return re.findall(rf"\b(\w+) JOIN ({'|'.join(LOOKUP_TABLES)})\b", sql)


@pytest.mark.parametrize("dimension", list(HOPS), ids=["one_hop", "two_hops", "three_hops"])
@pytest.mark.parametrize("aggregation", list(AGGREGATIONS))
def test_grouped_rows_add_up_to_the_total(runtime, gold, aggregation, dimension):
    expression, gold_value = AGGREGATIONS[aggregation]
    query = _query(expression, group_by=[dimension])

    grouped = _rows(runtime, query)

    assert grouped == pytest.approx(
        gold(f"SELECT {GOLD_FIELD[dimension]}, {gold_value} FROM orders AS o GROUP BY 1")
    )
    assert None in grouped  # a NULL key, a key with no record and a NULL attribute
    if aggregation in {"sum", "count"}:
        total = _rows(runtime, _query(expression))[None]
        assert sum(value or 0 for value in grouped.values()) == total
    assert _lookup_joins(_sql(runtime.config, query)) == [
        ("LEFT", table) for table in HOPS[dimension]
    ]


@pytest.mark.parametrize("condition", list(CONDITIONS))
def test_one_answer_however_the_filter_is_written(runtime, gold, condition):
    clause, gold_condition = CONDITIONS[condition]
    count = {"measure": "measure.geo.order_count"}
    expected = gold(f"SELECT COUNT(*) FROM orders AS o WHERE {gold_condition}")[None]

    as_where = _rows(runtime, _query(count, where=[clause]))[None]
    measure_filter = {**count, "kind": "aggregate", "filter": {"all": [clause]}}
    as_measure_filter = _rows(runtime, _query(measure_filter))[None]
    as_segment = runtime.segment_preview(f"segment.geo.{condition}")["member_count"]

    assert as_where == as_measure_filter == as_segment == expected
    entity, column = {REGION: ("region", "region_name"), COUNTRY: ("country", "country_name")}[
        clause["field"]
    ]
    op = "IS" if clause["op"] == "IS NULL" else clause["op"]
    as_aggregate_if = _aggregate_if(_comparison(_column(entity, column), op, clause.get("value")))
    if op == "IS":
        # A few reads still join a lookup inner (on ClickHouse, inside a metric predicate's own
        # query, beside a time role), so a condition a row with no match could satisfy is
        # refused, never answered differently by where it is read.
        with pytest.raises(SemanticLayerError) as raised:
            _rows(runtime, _query(as_aggregate_if))
        assert raised.value.details["reason"] == "null_accepting_condition"
    else:
        assert _rows(runtime, _query(as_aggregate_if))[None] == expected


# Orders of at least 10: every order but 3 and 4. It reads the orders alone.
EVERY_TENNER = {
    "expression": {
        "kind": "metric_predicate",
        "entity": "entity.geo_order",
        "scope_mode": "entity_only",
        "input": {"measure": "measure.geo.amount"},
        "op": ">=",
        "value": 10,
    },
    "op": "=",
    "value": True,
}


@pytest.mark.parametrize("dimension", list(HOPS), ids=["one_hop", "two_hops", "three_hops"])
def test_a_metric_filter_leaves_the_groups_adding_up(runtime, gold, dimension):
    query = _query({"measure": "measure.geo.amount"}, metric_filters=[EVERY_TENNER])

    grouped = _rows(runtime, {**query, "group_by": [dimension]})

    assert grouped == gold(
        f"SELECT {GOLD_FIELD[dimension]}, SUM(o.amount) FROM orders AS o"
        " WHERE o.amount >= 10 GROUP BY 1"
    )
    assert sum(grouped.values()) == _rows(runtime, query)[None] == 173  # 185 - 5 - 7
    assert _lookup_joins(_sql(runtime.config, {**query, "group_by": [dimension]})) == [
        ("LEFT", table) for table in HOPS[dimension]
    ]


# The orders that hold an item of each type, by country; the gold de-duplicates by order.
ORDERS_BY_TYPE = (
    "SELECT i.item_type, {field}, COUNT(DISTINCT o.order_id) FROM orders AS o"
    " JOIN items AS i ON i.order_id = o.order_id {where} GROUP BY 1, 2"
)
# Beverages: orders 1, 6 and 9 in Atlantis, 3 and 10 under NULL. Food: order 2 in Atlantis, and
# 4, 5 and 11 under NULL.
ORDERS_BY_TYPE_AND_COUNTRY = {
    ("beverage", "Atlantis"): 3,
    ("beverage", None): 2,
    ("food", "Atlantis"): 1,
    ("food", None): 3,
}
BEVERAGE = {"field": TYPE, "op": "=", "value": "beverage"}
ORDER_COUNT = {"measure": "measure.geo.order_count"}
POPULATION = "measure.geo.order_population"


def _scoped_population(*predicates: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "scoped_aggregate",
        "measure": POPULATION,
        "aggregation": "count_distinct",
        "predicates": list(predicates),
    }


def _order_predicate(measure: str, value: int) -> dict[str, Any]:
    return {"measure": measure, "entity": "entity.geo_order", "op": ">=", "value": value}


# Of each country's orders, the share of at least 15: a ratio of two scoped populations,
# which lowers to a snapshot of the orders joined to the sets they belong to.
SHARE_OF_BIG_ORDERS = {
    "kind": "ratio",
    "numerator": _scoped_population(
        _order_predicate("measure.geo.order_count", 1), _order_predicate("measure.geo.amount", 15)
    ),
    "denominator": _scoped_population(_order_predicate("measure.geo.order_count", 1)),
}


@dataclasses.dataclass(frozen=True)
class Leaf:
    query: dict[str, Any]
    marker: str  # in the rendered SQL of this leaf alone
    gold: str
    coarser: dict[str, Any] | None = None  # the same question with the lookup grouping removed
    rollup_safe: bool = False


LEAVES = {
    "root": Leaf(
        _query(ORDER_COUNT, group_by=[COUNTRY]),
        "FROM orders",
        f"SELECT {SQL_COUNTRY}, COUNT(*) FROM orders AS o GROUP BY 1",
        _query(ORDER_COUNT),
    ),
    "pre_aggregation_shortcut": Leaf(
        _query({"measure": "measure.geo.amount"}, group_by=[COUNTRY]),
        "_source_rollup",
        f"SELECT {SQL_COUNTRY}, SUM(o.amount) FROM orders AS o GROUP BY 1",
        _query({"measure": "measure.geo.amount"}),
    ),
    "de_duplicated": Leaf(
        _query(ORDER_COUNT, group_by=[TYPE, COUNTRY]),
        "_entity_rows",
        ORDERS_BY_TYPE.format(field=SQL_COUNTRY, where=""),
        _query(ORDER_COUNT, group_by=[TYPE]),
    ),
    "entity_in_terms_of": Leaf(
        _query(ORDER_COUNT, group_by=[TYPE, COUNTRY]),
        "FROM items",
        ORDERS_BY_TYPE.format(field=SQL_COUNTRY, where=""),
        _query(ORDER_COUNT, group_by=[TYPE]),
        rollup_safe=True,
    ),
    "exists": Leaf(
        _query(ORDER_COUNT, group_by=[COUNTRY], where=[BEVERAGE]),
        "EXISTS (",
        f"SELECT {SQL_COUNTRY}, COUNT(*) FROM orders AS o WHERE EXISTS (SELECT 1 FROM items"
        " AS i WHERE i.order_id = o.order_id AND i.item_type = 'beverage') GROUP BY 1",
        _query(ORDER_COUNT, where=[BEVERAGE]),
    ),
    "snapshot": Leaf(
        _query(SHARE_OF_BIG_ORDERS, group_by=[COUNTRY], time=MONTHLY),
        "_snapshot",
        f"SELECT {SQL_COUNTRY}, AVG(CASE WHEN o.amount >= 15 THEN 1.0 ELSE 0.0 END)"
        " FROM orders AS o GROUP BY 1",
    ),
}


def _leaf_violations(runtime: Runtime, gold: Any, leaf: Leaf) -> list[str]:
    """How the leaf fails the rule: a lookup joined inner, or rows lost from its groups."""
    sql = _sql(runtime.config, leaf.query)
    violations = [] if leaf.marker in sql else [f"not this leaf: {leaf.marker!r} not in SQL"]
    joins = _lookup_joins(sql)
    if not joins or any(kind != "LEFT" for kind, _ in joins):
        violations.append(f"lookup joins {joins}")
    grouped = _rows(runtime, leaf.query)
    if grouped != pytest.approx(gold(leaf.gold)):
        violations.append(f"{grouped} is not gold")
    if leaf.coarser is not None:
        coarser = _rows(runtime, leaf.coarser)
        rolled: dict[Any, float] = {}
        for group, value in grouped.items():
            key = group[0] if isinstance(group, tuple) else None  # without the lookup grouping
            rolled[key] = rolled.get(key, 0.0) + (value or 0.0)
        if rolled != pytest.approx(coarser):
            violations.append(f"groups {rolled} do not add up to {coarser}")
    return violations


@pytest.mark.parametrize("name", list(LEAVES))
def test_every_leaf_keeps_the_rows(runtime, rollup_safe_runtime, gold, name):
    leaf = LEAVES[name]

    violations = _leaf_violations(rollup_safe_runtime if leaf.rollup_safe else runtime, gold, leaf)

    assert violations == []


def _inner_lookups(*args: Any, **kwargs: Any) -> list[SqlJoin]:
    joins = _joins_for_paths(*args, **kwargs)
    return [dataclasses.replace(join, join_type="INNER") for join in joins]


_joins_for_paths = sql_lowering._joins_for_paths


@pytest.mark.parametrize("name", list(LEAVES))
def test_a_leaf_that_joins_its_lookups_inner_fails_the_check(
    package, tmp_path, gold, monkeypatch, name
):
    """The check above catches a leaf that bypasses the rule: here every leaf joins INNER."""
    leaf = LEAVES[name]
    monkeypatch.setattr(sql_lowering, "_joins_for_paths", _inner_lookups)
    runtime = Runtime.from_path(str(_write_package(tmp_path, rollup_safe=leaf.rollup_safe)))
    try:
        violations = _leaf_violations(runtime, gold, leaf)
    finally:
        runtime.close()

    assert any(text.startswith("lookup joins") for text in violations)
    assert any("is not gold" in text for text in violations)


OPERATORS = {
    "=": ("North", "= 'North'"),
    "!=": ("North", "<> 'North'"),
    "IN": (["North", "East"], "IN ('North', 'East')"),
    "NOT IN": (["North", "East"], "NOT IN ('North', 'East')"),
    "IS NULL": (None, "IS NULL"),
}
# Per leaf: the query without the region filter, and the gold with a {condition} to fill in.
FILTERED_LEAVES = {
    "root": (_query(ORDER_COUNT), "SELECT COUNT(*) FROM orders AS o WHERE {condition}", False),
    "de_duplicated": (
        _query(ORDER_COUNT, group_by=[TYPE]),
        "SELECT i.item_type, COUNT(DISTINCT o.order_id) FROM orders AS o JOIN items AS i"
        " ON i.order_id = o.order_id WHERE {condition} GROUP BY 1",
        False,
    ),
    "entity_in_terms_of": (
        _query(ORDER_COUNT, group_by=[TYPE]),
        "SELECT i.item_type, COUNT(DISTINCT o.order_id) FROM orders AS o JOIN items AS i"
        " ON i.order_id = o.order_id WHERE {condition} GROUP BY 1",
        True,
    ),
    "exists": (
        _query(ORDER_COUNT, where=[BEVERAGE]),
        "SELECT COUNT(*) FROM orders AS o WHERE {condition} AND EXISTS (SELECT 1 FROM items"
        " AS i WHERE i.order_id = o.order_id AND i.item_type = 'beverage')",
        False,
    ),
}


@pytest.mark.parametrize("op", list(OPERATORS))
@pytest.mark.parametrize("name", list(FILTERED_LEAVES))
def test_filters_on_a_looked_up_field_follow_sql_null_rules_in_each_leaf(
    runtime, rollup_safe_runtime, gold, name, op
):
    """`IS NULL` selects the rows the region lookup found no match for (orders 6, 7, 8, 10
    and 11); `=`, `!=`, `IN` and `NOT IN` never match them."""
    query, gold_sql, rollup_safe = FILTERED_LEAVES[name]
    value, sql = OPERATORS[op]
    clause = {"field": REGION, "op": op, **({"value": value} if value is not None else {})}
    filtered = {**query, "where": [*query.get("where", []), clause]}

    got = _rows(rollup_safe_runtime if rollup_safe else runtime, filtered)

    expected = gold(gold_sql.format(condition=f"{SQL_REGION} {sql}"))
    assert got == pytest.approx({key: value for key, value in expected.items() if value})


def test_a_dimension_only_query_lists_the_rows_with_no_match(runtime, gold):
    rows = runtime.query({"version": 1, "group_by": [SEGMENT, COUNTRY]})["rows"]

    pairs = {(row[SEGMENT], row[COUNTRY]) for row in rows}
    customers = (
        "SELECT c.segment, (SELECT k.country_name FROM regions AS r, countries AS k WHERE"
        " r.region_id = c.region_id AND k.country_id = r.country_id), 1 FROM customers AS c"
    )
    assert pairs == set(gold(customers))
    assert (None, "Atlantis") in pairs and ("retail", None) in pairs


def test_the_entity_in_terms_of_time_role_keeps_the_inner_join(rollup_safe_runtime):
    """The time role is the order's, read from the items through the order hop: that hop stays
    inner, as a time role read through a lookup does in every leaf, and the rest keep rows."""
    query = _query(ORDER_COUNT, group_by=[TYPE, COUNTRY], time=MONTHLY)

    sql = _sql(rollup_safe_runtime.config, query)

    assert "FROM items" in sql
    assert re.findall(r"\b(\w+) JOIN (orders|customers|regions|countries)\b", sql) == [
        ("INNER", "orders"),
        ("LEFT", "customers"),
        ("LEFT", "regions"),
        ("LEFT", "countries"),
    ]
    # Every order is in January, and every item has its order: no row is lost.
    assert _rows(rollup_safe_runtime, query) == ORDERS_BY_TYPE_AND_COUNTRY


def test_a_lookup_after_a_one_to_many_hop_joins_left_inside_exists(runtime, gold):
    """A child filter reads the item's product: the lookup joins LEFT inside the EXISTS that
    reads the items, not as an EXISTS of its own."""
    query = _query(ORDER_COUNT, where=[{"field": CATEGORY, "op": "=", "value": "hot"}])

    sql = _sql(runtime.config, query)

    assert (
        "EXISTS ( SELECT 1 AS match FROM items LEFT JOIN products ON items.sku = products.sku"
        " WHERE orders.order_id = items.order_id AND products.category = 'hot' )"
    ) in sql
    assert (
        _rows(runtime, query)[None]
        == gold(
            "SELECT COUNT(*) FROM orders AS o WHERE EXISTS (SELECT 1 FROM items AS i WHERE"
            " i.order_id = o.order_id AND (SELECT p.category FROM products AS p"
            " WHERE p.sku = i.sku) = 'hot')"
        )[None]
    )


@pytest.mark.parametrize("name", list(LEAVES))
def test_clickhouse_keeps_every_lookup_inner(package, name):
    """ClickHouse reads '' or 0, not NULL, from an unmatched outer-join column, so its lookups
    stay inner in every leaf."""
    leaf = LEAVES[name]
    base = load_package_config(str(package))
    config = dataclasses.replace(
        base, package=dataclasses.replace(base.package, warehouse="clickhouse")
    )
    joins = _lookup_joins(_sql(config, leaf.query))

    assert joins and all(kind == "INNER" for kind, _ in joins)


def _callers(function: str) -> set[str]:
    """``module:function`` for every function in the engine that calls ``function``."""
    root = Path(__file__).resolve().parents[2] / "semantic_rails"
    callers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == function
                ):
                    callers.add(f"{path.stem}:{node.name}")
    return callers


def test_one_place_decides_how_a_relationship_joins():
    """Every leaf joins its paths through ``_joins_for_paths``, the one caller of the join
    condition builder, so no leaf can choose a lookup's join type on its own; and only a
    metric predicate's own query asks it to join lookups INNER."""
    assert _callers("_join_on_for_relationship") == {"paths:_joins_for_paths"}
    assert _callers("inner_lookups") == {"compiler:_compile_predicate_source_ast"}
