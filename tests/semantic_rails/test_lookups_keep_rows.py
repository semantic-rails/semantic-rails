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
from semantic_rails.ir import PathSelection
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import AggregateRelationConfig
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

# Per segment name: a filter clause, and the same condition in gold SQL.
CONDITIONS = {
    "no_region": ({"field": REGION, "op": "IS NULL"}, f"{SQL_REGION} IS NULL"),
    "north": ({"field": REGION, "op": "=", "value": "North"}, f"{SQL_REGION} = 'North'"),
    "no_country": ({"field": COUNTRY, "op": "IS NULL"}, f"{SQL_COUNTRY} IS NULL"),
}


def _write_package(root: Path, *, rollup_safe: bool = False, extra_seed: str = "") -> Path:
    pkg = root / "geo"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "segments").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL + extra_seed)
    (pkg / "package.yml").write_text(PACKAGE)
    (pkg / "graph.yml").write_text(GRAPH + (ROLLUP_SAFE_ITEMS if rollup_safe else ""))
    for name, body in MODELS.items():
        (pkg / "models" / f"{name}.yml").write_text(textwrap.dedent(body))
    recipe = {
        "as": "metric.geo.order_amount",
        "kind": "derived",
        "expression": {"measure": "measure.geo.amount"},
    }
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
SQL_ORDERS_BY_TYPE_AND_COUNTRY = (
    f"SELECT i.item_type, {SQL_COUNTRY}, COUNT(DISTINCT o.order_id) FROM orders AS o"
    " JOIN items AS i ON i.order_id = o.order_id GROUP BY 1, 2"
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
        SQL_ORDERS_BY_TYPE_AND_COUNTRY,
        _query(ORDER_COUNT, group_by=[TYPE]),
    ),
    "entity_in_terms_of": Leaf(
        _query(ORDER_COUNT, group_by=[TYPE, COUNTRY]),
        "FROM items",
        SQL_ORDERS_BY_TYPE_AND_COUNTRY,
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


def _region_amounts(group: str = "") -> str:
    """Each region's amount, through the lookups: R1 53 (orders 1, 2 and 9), R2 5, R3 7, R4 11
    and R5 13. Orders 7, 8, 10 and 11 (96 in all) reach no region."""
    return (
        f"SELECT {group}r.region_id, SUM(o.amount) AS v FROM orders AS o, customers AS c,"
        " regions AS r WHERE c.customer_id = o.customer_id AND r.region_id = c.region_id"
        " GROUP BY ALL"
    )


DISTRIBUTIONS = {"median": "MEDIAN(v)", "avg": "AVG(v)", "percentile": "QUANTILE_CONT(v, 0.25)"}


def _distribution(function: str) -> dict[str, Any]:
    return {
        "kind": "distribution",
        "function": function,
        **({"p": 0.25} if function == "percentile" else {}),
        "over": {
            "kind": "entity_value",
            "entity": "entity.geo_region",
            "input": {"measure": "measure.geo.amount"},
        },
    }


@pytest.mark.parametrize("function", list(DISTRIBUTIONS))
def test_a_distribution_reads_only_the_entities_its_lookups_find(runtime, gold, function):
    """The regions' values are [53, 5, 7, 11, 13], so the median is 11. The orders that reach
    no region are no entity, never a sixth NULL region worth 96 (median 12). Beside it, the
    plain amount grouped by segment keeps its NULL group (orders 9, 10 and 11)."""
    aggregate = DISTRIBUTIONS[function]

    alone = _rows(runtime, _query(_distribution(function)))[None]

    assert alone == pytest.approx(gold(f"SELECT {aggregate} FROM ({_region_amounts()})")[None])
    if function == "median":
        assert alone == 11
    query = {
        "version": 1,
        "select": [
            {"as": "value", "expression": _distribution(function)},
            {"as": "amount", "expression": {"measure": "measure.geo.amount"}},
        ],
        "group_by": [SEGMENT],
    }
    rows = runtime.query(query)["rows"]
    by_segment = gold(
        f"SELECT segment, {aggregate} FROM ({_region_amounts('c.segment, ')}) GROUP BY 1"
    )
    assert {row[SEGMENT]: _number(row["value"]) for row in rows} == pytest.approx(by_segment)
    assert {row[SEGMENT]: _number(row["amount"]) for row in rows} == gold(
        f"SELECT {SQL_SEGMENT}, SUM(o.amount) FROM orders AS o GROUP BY 1"
    )
    assert by_segment[None] == pytest.approx(23)  # order 9 alone: C8 has no segment


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


# Item 111 names order 999, which has no record.
ORPHAN_ITEM_SEED = "INSERT INTO items VALUES (111, 999, 'coffee', 'beverage');\n"


def test_the_entity_in_terms_of_leaf_counts_no_order_for_an_item_with_no_order(tmp_path):
    """Counted from the items, item 111 is none of the orders: the hop back to the order joins
    INNER, and the lookups past it keep rows (orders 3 and 10 still count under NULL)."""
    query = _query(ORDER_COUNT, group_by=[TYPE, COUNTRY])
    runtime = Runtime.from_path(
        str(_write_package(tmp_path, rollup_safe=True, extra_seed=ORPHAN_ITEM_SEED))
    )
    connection = duckdb.connect()
    try:
        sql = _sql(runtime.config, query)
        rows = _rows(runtime, query)
        connection.execute(SEED_SQL + ORPHAN_ITEM_SEED)
        gold_rows = connection.execute(SQL_ORDERS_BY_TYPE_AND_COUNTRY).fetchall()
    finally:
        runtime.close()
        connection.close()

    assert "FROM items" in sql
    assert re.findall(r"\b(\w+) JOIN (orders|customers|regions|countries)\b", sql) == [
        ("INNER", "orders"),
        ("LEFT", "customers"),
        ("LEFT", "regions"),
        ("LEFT", "countries"),
    ]
    assert rows == {(kind, country): count for kind, country, count in gold_rows}
    assert rows == ORDERS_BY_TYPE_AND_COUNTRY


@pytest.mark.parametrize(
    "sku", ["NULL", "'missing'", "'coffee'"], ids=["null", "unmatched", "matched"]
)
@pytest.mark.parametrize("with_time", [False, True], ids=["no_time", "monthly"])
@pytest.mark.parametrize("rollup_safe", [False, True], ids=["de_duplicated", "entity_in_terms_of"])
def test_a_child_lookup_grouping_counts_only_existing_parents(
    tmp_path, sku, with_time, rollup_safe
):
    """Both leaves count existing orders, even when an orphan item's product lookup finds no
    match. Reading the order's time axis preserves that count and the valid NULL group."""
    seed = f"INSERT INTO items VALUES (111, 999, {sku}, 'beverage');\n"
    query = _query(ORDER_COUNT, group_by=[CATEGORY], **({"time": MONTHLY} if with_time else {}))
    runtime = Runtime.from_path(
        str(_write_package(tmp_path, rollup_safe=rollup_safe, extra_seed=seed))
    )
    connection = duckdb.connect()
    try:
        sql = _sql(runtime.config, query)
        rows = _rows(runtime, query)
        connection.execute(SEED_SQL + seed)
        expected = dict(
            connection.execute(
                "SELECT (SELECT p.category FROM products AS p WHERE p.sku = i.sku),"
                " COUNT(DISTINCT o.order_id) FROM orders AS o"
                " JOIN items AS i ON i.order_id = o.order_id GROUP BY 1"
            ).fetchall()
        )
    finally:
        runtime.close()
        connection.close()

    assert rows == expected == {"hot": 4, "cold": 2, None: 3}
    assert _lookup_joins(sql) == [("LEFT", "products")]
    if rollup_safe:
        assert "FROM items" in sql
        assert "INNER JOIN orders ON items.order_id = orders.order_id" in sql
    else:
        assert "_entity_rows" in sql


def _write_parent_roles_package(
    root: Path, *, extra_seed: str, other_preference: int, pin_other: bool = False
) -> Path:
    package = _write_package(
        root,
        rollup_safe=True,
        extra_seed=(
            "ALTER TABLE items ADD COLUMN other_order_id INTEGER;\n"
            "UPDATE items SET other_order_id = order_id;\n" + extra_seed
        ),
    )
    graph = yaml.safe_load((package / "graph.yml").read_text())["graph"]
    graph["relationships"]["items_other_order"] = {
        "id": "relationship.items_other_order",
        "entities": ["item", "order"],
        "via": "other_order_id",
        "cardinality": "many_to_one",
        "path_preference": other_preference,
    }
    graph["path_preferences"] = [
        {
            "source_entity": "order",
            "target_entity": "product",
            "relationship_path": ["relationship.items_order", "relationship.items_product"],
        }
    ]
    if pin_other:
        graph["path_preferences"].append(
            {
                "source_entity": "item",
                "target_entity": "order",
                "relationship_path": ["relationship.items_other_order"],
            }
        )
    (package / "graph.yml").write_text(yaml.safe_dump({"graph": graph}))
    return package


@pytest.mark.parametrize(
    ("other_preference", "pin_other"),
    [(0, False), (100, False), (100, True)],
    ids=["preferred_other", "tied_parents", "pinned_other"],
)
@pytest.mark.parametrize(
    ("extra_seed", "expected"),
    [
        pytest.param(
            "INSERT INTO items VALUES (111, 999, 'coffee', 'beverage', 1);\n",
            {"hot": 4, "cold": 2, None: 3},
            id="orphan_with_another_existing_parent",
        ),
        pytest.param(
            "UPDATE items SET other_order_id = NULL WHERE order_id = 1;\n",
            {"hot": 4, "cold": 2, None: 3},
            id="valid_parent_with_null_other_parent",
        ),
        pytest.param(
            "DELETE FROM items;\nINSERT INTO items VALUES (111, 1, 'coffee', 'beverage', 999);\n",
            {"hot": 1},
            id="valid_parent_with_missing_other_parent",
        ),
    ],
)
def test_a_parent_count_with_two_relationships_uses_the_counted_path(
    tmp_path, extra_seed, expected, other_preference, pin_other
):
    """A preferred or ambiguous alternative parent never changes the pinned order count."""
    package = _write_parent_roles_package(
        tmp_path, extra_seed=extra_seed, other_preference=other_preference, pin_other=pin_other
    )
    runtime = Runtime.from_path(str(package))
    connection = duckdb.connect()
    try:
        query = _query(ORDER_COUNT, group_by=[CATEGORY])
        sql = _sql(runtime.config, query)
        connection.execute((package / "data" / "seed.sql").read_text())
        reference = dict(
            connection.execute(
                "SELECT (SELECT p.category FROM products AS p WHERE p.sku = i.sku),"
                " COUNT(DISTINCT o.order_id) FROM orders AS o"
                " JOIN items AS i ON i.order_id = o.order_id GROUP BY 1"
            ).fetchall()
        )
        assert _rows(runtime, query) == reference == expected
    finally:
        runtime.close()
        connection.close()

    assert "FROM orders" in sql
    assert "FROM items" not in sql
    assert "other_order_id" not in sql
    assert _lookup_joins(sql) == [("LEFT", "products")]


@pytest.mark.parametrize("dimension", [TYPE, CATEGORY], ids=["child", "child_lookup"])
def test_a_reverse_only_parent_relationship_declines_the_child_anchor(tmp_path, dimension):
    """A supported parent-to-child grouping needs no prohibited child-to-parent lookup."""
    package = _write_package(tmp_path, rollup_safe=True, extra_seed=ORPHAN_ITEM_SEED)
    graph = yaml.safe_load((package / "graph.yml").read_text())
    graph["graph"]["relationships"]["items_order"]["allowed_directions"] = ["reverse"]
    (package / "graph.yml").write_text(yaml.safe_dump(graph))
    runtime = Runtime.from_path(str(package))
    connection = duckdb.connect()
    try:
        query = _query(ORDER_COUNT, group_by=[dimension])
        sql = _sql(runtime.config, query)
        connection.execute(SEED_SQL + ORPHAN_ITEM_SEED)
        grouping = (
            "i.item_type"
            if dimension == TYPE
            else "(SELECT p.category FROM products AS p WHERE p.sku = i.sku)"
        )
        reference = dict(
            connection.execute(
                f"SELECT {grouping}, COUNT(DISTINCT o.order_id) FROM orders AS o"
                " JOIN items AS i ON i.order_id = o.order_id GROUP BY 1"
            ).fetchall()
        )
        assert _rows(runtime, query) == reference
    finally:
        runtime.close()
        connection.close()

    assert "FROM orders" in sql
    assert "FROM items" not in sql


@pytest.mark.parametrize("bypass", ["missing", "left"])
def test_a_child_anchored_count_refuses_a_bypassed_parent_check(tmp_path, monkeypatch, bypass):
    """The shared join builder refuses the leaf if its parent check is absent or nullable."""
    from semantic_rails.compiler_parts import paths

    config = load_package_config(str(_write_package(tmp_path, rollup_safe=True)))
    if bypass == "missing":
        anchor_plan = sql_lowering._entity_in_terms_of_anchor_plan

        def without_parent_check(*args):
            result = anchor_plan(*args)
            assert result is not None
            result["path_selections"] = [
                row for row in result["path_selections"] if row.purpose != "entity_in_terms_of_root"
            ]
            return result

        monkeypatch.setattr(sql_lowering, "_entity_in_terms_of_anchor_plan", without_parent_check)
    else:
        monkeypatch.setattr(
            paths,
            "_INNER_LOOKUP_PURPOSES",
            paths._INNER_LOOKUP_PURPOSES - {"entity_in_terms_of_root"},
        )

    with pytest.raises(SemanticLayerError) as raised:
        _sql(config, _query(ORDER_COUNT, group_by=[CATEGORY]))

    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details["measure_entity"] == "entity.geo_order"
    assert raised.value.details["source_entity"] == "entity.geo_item"


@pytest.mark.parametrize("relationship", ["items_order", "items_other_order"])
def test_a_child_anchor_guard_refuses_two_parent_relationships(tmp_path, relationship):
    """Bypassing the anchor's decline cannot pass the guard just by joining orders INNER."""
    from semantic_rails.compiler_parts import paths

    config = load_package_config(
        str(_write_parent_roles_package(tmp_path, extra_seed="", other_preference=0))
    )
    root = PathSelection(
        target_entity="entity.geo_order",
        purpose="entity_in_terms_of_root",
        chosen_path=[f"relationship.{relationship}"],
        candidate_paths=[],
        analysis={},
    )

    with pytest.raises(SemanticLayerError) as raised:
        paths._joins_for_paths("entity.geo_item", [root], config, measure_entity="entity.geo_order")

    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details["measure_entity"] == "entity.geo_order"
    assert raised.value.details["source_entity"] == "entity.geo_item"


def test_the_entity_in_terms_of_leaf_leaves_a_rollup_dimension_to_the_order_s_leaf(tmp_path, gold):
    """A rollup of the orders holds the country, so the base answers as that rollup does: every
    hop to the country is INNER. Orders 4, 5 and 11 reach no country record and drop out; order
    3's country has no name. Counted from the items, the leaf would not see the orders' rollup,
    so the orders' own leaf answers."""
    base = load_package_config(str(_write_package(tmp_path, rollup_safe=True)))
    rollup = AggregateRelationConfig(
        id="aggregate_relation.geo_orders_by_country",
        relation="orders_by_country",
        source_entity="entity.geo_order",
        dimensions=[COUNTRY],
    )
    config = dataclasses.replace(base, aggregate_relations=[rollup])

    sql = _sql(config, _query(ORDER_COUNT, group_by=[TYPE, COUNTRY]))

    assert "FROM orders" in sql
    assert _lookup_joins(sql) == [
        ("INNER", table) for table in ("customers", "regions", "countries")
    ]
    expected = gold(
        "SELECT i.item_type, k.country_name, COUNT(DISTINCT o.order_id) FROM orders AS o"
        " JOIN items AS i ON i.order_id = o.order_id"
        " JOIN customers AS c ON c.customer_id = o.customer_id"
        " JOIN regions AS r ON r.region_id = c.region_id"
        " JOIN countries AS k ON k.country_id = r.country_id GROUP BY 1, 2"
    )
    assert gold(sql) == expected
    assert expected == {("beverage", "Atlantis"): 3, ("beverage", None): 1, ("food", "Atlantis"): 1}


def _with_rollup(config: Any, source_entity: str) -> Any:
    rollup = AggregateRelationConfig(
        id="aggregate_relation.geo_by_country",
        relation="by_country",
        source_entity=source_entity,
        dimensions=[COUNTRY],
    )
    return dataclasses.replace(config, aggregate_relations=[rollup])


@pytest.mark.parametrize("item_rollup", [False, True], ids=["no_rollup", "item_rollup"])
@pytest.mark.parametrize(
    ("rollup_safe", "marker"),
    [(False, "_entity_rows"), (True, "FROM items")],
    ids=["de_duplicated", "entity_in_terms_of"],
)
def test_a_rollup_of_another_model_changes_no_leaf(
    tmp_path, gold, rollup_safe, marker, item_rollup
):
    """A rollup of the items that holds the country belongs to item measures: the order count
    joins its lookups as the base tables do from either leaf, with both NULL groups."""
    config = load_package_config(str(_write_package(tmp_path, rollup_safe=rollup_safe)))
    if item_rollup:
        config = _with_rollup(config, "entity.geo_item")

    sql = _sql(config, _query(ORDER_COUNT, group_by=[TYPE, COUNTRY]))

    assert marker in sql
    assert _lookup_joins(sql) == [
        ("LEFT", table) for table in ("customers", "regions", "countries")
    ]
    assert gold(sql) == gold(SQL_ORDERS_BY_TYPE_AND_COUNTRY) == ORDERS_BY_TYPE_AND_COUNTRY


def test_a_dimension_only_query_reads_no_rollup(package, gold):
    """No measure is read, so no rollup applies: the items rollup holding the country leaves
    the listing's NULL country for food (items 105, 106 and 109) in."""
    config = _with_rollup(load_package_config(str(package)), "entity.geo_item")

    sql = _sql(config, {"version": 1, "group_by": [TYPE, COUNTRY]})

    assert "FROM items" in sql
    assert _lookup_joins(sql) == [
        ("LEFT", table) for table in ("customers", "regions", "countries")
    ]
    item_country = (
        "(SELECT k.country_name FROM orders AS o, customers AS c, regions AS r, countries AS k"
        " WHERE o.order_id = i.order_id AND c.customer_id = o.customer_id"
        " AND r.region_id = c.region_id AND k.country_id = r.country_id)"
    )
    expected = gold(f"SELECT DISTINCT i.item_type, {item_country}, 1 FROM items AS i")
    assert gold(f"SELECT *, 1 FROM ({sql})") == expected
    assert ("food", None) in expected


def test_a_leaf_reading_another_model_s_rows_refuses_the_measure_s_rollup(tmp_path, monkeypatch):
    """The guard behind the entity_in_terms_of leaf's decline: with the decline bypassed, the
    joins refuse to answer a dimension the orders' rollup holds from the items' rows."""
    config = _with_rollup(
        load_package_config(str(_write_package(tmp_path, rollup_safe=True))), "entity.geo_order"
    )
    monkeypatch.setattr(sql_lowering, "rollup_held_lookups", lambda *args: set())

    with pytest.raises(SemanticLayerError) as raised:
        _sql(config, _query(ORDER_COUNT, group_by=[TYPE, COUNTRY]))

    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details["measure_entity"] == "entity.geo_order"
    assert raised.value.details["source_entity"] == "entity.geo_item"


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
    condition builder, so no leaf can choose a lookup's join type on its own; only a metric
    predicate's own query and a distribution's per-entity values ask it to join lookups INNER;
    and only it reads which lookups a rollup holds, for the rollup's own model."""
    assert _callers("_join_on_for_relationship") == {"paths:_joins_for_paths"}
    assert _callers("inner_lookups") == {
        "compiler:_compile_predicate_source_ast",
        "sql_lowering:_distribution_select",
    }
    assert _callers("rollup_dimension_entities") == {
        "paths:_joins_for_paths",
        "paths:rollup_held_lookups",
    }
