"""Lookups keep the rows they find no match for, in every query shape.

An N:1 or 1:1 hop never removes a measure's row, whatever reads the looked-up field (a
grouping, a filter, the measure's own filter, an aggregate_if or a measure expression) and in
every leaf (the plain leaf, the pre-aggregation shortcut, the de-duplicated leaf, the
entity_in_terms_of leaf, the EXISTS leaf and the snapshot of an entity-set ratio). A row whose
foreign key is NULL or matches no row stays, with NULL for everything the hop looks up: it
groups under NULL, so grouped rows add up to the ungrouped total, `missing` selects it, `not`
keeps it and `is` never matches it, wherever the filter is written.

Fixture: orders -> customer -> region -> country, three lookup hops. At each hop one row has a
NULL foreign key, one a foreign key with no record, and one a match whose attribute is NULL:
order 10 has no customer, order 11 a customer with no record (C9) and C8 no segment; C6 has no
region, C7 a region with no record (R9) and R5 no name; R3 has no country, R4 a country with no
record (K9) and K2 no name. Orders hold items (one-to-many), each a lookup of a product: item
104 has no product, item 105 one with no record and melt no category; item 111 names an order
with no record (999). Gold values come from SQL written independently of the engine: scalar
subqueries read NULL where a lookup finds no row, with no joins.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import (
    leaves,
    lowering_common,
    paths,
    physical_plan,
    plan_checks,
    predicate_sets,
    sql_lowering,
)
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.ir import PathSelection
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import AggregateRelationConfig
from semantic_rails.sql_ast import SqlJoin
from tests.semantic_rails.conftest import opened

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
      as: relationship.items_order
      entities: [item, order]
      cardinality: many_to_one
      rollup_safe:
        reverse: [count_distinct]
"""

# Each model's id and relation are its name.
MODELS = {
    "countries": """
        entities: {country: {}}
        dimensions: {country_name: {label: Country, kind: categorical}}
        """,
    "regions": """
        entities: {region: {}, country: {}}
        dimensions: {region_name: {label: Region, kind: categorical}}
        """,
    "customers": """
        entities: {customer: {}, region: {}}
        dimensions: {segment: {label: Segment, kind: categorical}}
        """,
    "orders": """
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
        entities: {item: {}, order: {}, product: {expr: sku}}
        dimensions: {item_type: {label: Item type, kind: categorical}}
        """,
    "products": """
        entities: {product: {}}
        dimensions: {category: {label: Category, kind: categorical}}
        """,
}

SEGMENT = "dimension.geo_customer_segment"
REGION = "dimension.geo_region_region_name"
COUNTRY = "dimension.geo_country_country_name"
TYPE = "dimension.geo_item_item_type"
CATEGORY = "dimension.geo_product_category"
ORDER_COUNT = "measure.geo.order_count"
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


def _count_if(column: dict[str, Any], op: str, value: Any) -> dict[str, Any]:
    """An aggregate_if counting the orders whose ``column`` compares to ``value``."""
    right = {"kind": "literal", "value": value}
    return {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": {"kind": "comparison", "op": op, "left": column, "right": right},
        "value": _column("order", "order_id"),
    }


# (expression, gold aggregate over orders AS o). The count's condition reads the order alone,
# so a grouping two hops away takes the pre-aggregation shortcut, as the sum does.
AGGREGATIONS = {
    "sum": ("measure.geo.amount", "SUM(o.amount)"),
    "count": (_count_if(_column("order", "amount"), ">", 0), "COUNT(o.order_id)"),
    "count_distinct": ("measure.geo.order_count", "COUNT(DISTINCT o.order_id)"),
    "avg": (
        {"kind": "aggregate", "measure": "measure.geo.amount", "aggregation": "avg"},
        "AVG(o.amount)",
    ),
}

# Per segment name: a filter clause, and the same condition in gold SQL.
CONDITIONS = {
    "no_region": ({"field": REGION, "op": "missing", "value": True}, f"{SQL_REGION} IS NULL"),
    "north": ({"field": REGION, "op": "is", "value": "North"}, f"{SQL_REGION} = 'North'"),
    "no_country": ({"field": COUNTRY, "op": "missing", "value": True}, f"{SQL_COUNTRY} IS NULL"),
}
METRICS = """
metrics:
  geo.order_amount: {as: metric.geo.order_amount, kind: derived, value_type: number,
    expression: {measure: measure.geo.amount}}
"""


def _write_package(
    root: Path, *, rollup_safe: bool = False, extra_seed: str = "", graph: str = ""
) -> Path:
    """``graph`` is YAML appended to the rollup-safe graph, after the items' relationship."""
    pkg = root / "geo"
    for folder in ("data", "models", "segments"):
        (pkg / folder).mkdir(parents=True)
    (pkg / "data" / "seed.sql").write_text(SEED_SQL + extra_seed)
    (pkg / "package.yml").write_text(PACKAGE)
    (pkg / "graph.yml").write_text(GRAPH + (ROLLUP_SAFE_ITEMS + graph if rollup_safe else ""))
    for name, body in MODELS.items():
        model = {"id": name, "relation": name, **yaml.safe_load(body)}
        (pkg / "models" / f"{name}.yml").write_text(yaml.safe_dump({"model": model}))
    (pkg / "metrics.yml").write_text(METRICS)
    segments = {
        name: {
            "as": f"segment.geo.{name}",
            "label": name,
            "entity": "entity.geo_order",
            "basis_metric": "metric.geo.order_amount",
            "membership": {"where": [clause]},
        }
        for name, (clause, _) in CONDITIONS.items()
    }
    (pkg / "segments" / "core.yml").write_text(yaml.safe_dump({"segments": segments}))
    return pkg


# Item 111 names order 999, which has no record: the shared packages and the gold hold it.
ORPHAN_ITEM_SEED = "INSERT INTO items VALUES (111, 999, 'coffee', 'beverage');\n"


def _runtime(root: Path, rollup_safe: bool) -> Iterator[Runtime]:
    package = _write_package(root, rollup_safe=rollup_safe, extra_seed=ORPHAN_ITEM_SEED)
    runtime = Runtime.from_path(str(package))
    yield opened(runtime)
    runtime.close()


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    yield from _runtime(tmp_path_factory.mktemp("lookups"), rollup_safe=False)


@pytest.fixture(scope="module")
def rollup_safe_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    yield from _runtime(tmp_path_factory.mktemp("rollup_safe"), rollup_safe=True)


@pytest.fixture(scope="module")
def gold() -> Iterator[Any]:
    connection = duckdb.connect()
    connection.execute(SEED_SQL + ORPHAN_ITEM_SEED)

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


def _query(expression: Any, **extra: Any) -> dict[str, Any]:
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
    engine, _ = read_call(query, config=config)
    return " ".join(compile_query(config, Registry(config), engine)["sql"].split())


def _lookup_joins(sql: str) -> list[tuple[str, str]]:
    return re.findall(rf"\b(\w+) JOIN ({'|'.join(LOOKUP_TABLES)})\b", sql)


# Orders of at least 10: every order but 3 and 4. It reads the orders alone.
EVERY_TENNER = {
    "entity": "entity.geo_order",
    "having": [{"field": "measure.geo.amount", "op": "gte", "value": 10}],
    "over": "all_time",
}


@pytest.mark.parametrize(
    ("aggregation", "dimension", "metric_filter"),
    [
        # The sum and the distinct count by country are leaves below.
        ("count", COUNTRY, False),
        ("avg", COUNTRY, False),
        ("sum", SEGMENT, False),
        ("count_distinct", REGION, False),
        # A metric filter leaves the groups adding up too.
        ("sum", COUNTRY, True),
    ],
)
def test_grouped_rows_add_up_to_the_total(runtime, gold, aggregation, dimension, metric_filter):
    expression, gold_value = AGGREGATIONS[aggregation]
    where = {"where": [EVERY_TENNER]} if metric_filter else {}
    gold_where = "WHERE o.amount >= 10" if metric_filter else ""
    query = _query(expression, group_by=[dimension], **where)

    grouped = _rows(runtime, query)

    assert grouped == pytest.approx(
        gold(
            f"SELECT {GOLD_FIELD[dimension]}, {gold_value} FROM orders AS o {gold_where} GROUP BY 1"
        )
    )
    assert None in grouped  # a NULL key, a key with no record and a NULL attribute
    if aggregation in {"sum", "count"}:
        total = _rows(runtime, _query(expression, **where))[None]
        assert sum(value or 0 for value in grouped.values()) == total
    assert _lookup_joins(_sql(runtime.config, query)) == [
        ("LEFT", table) for table in HOPS[dimension]
    ]


@pytest.mark.parametrize("condition", list(CONDITIONS))
def test_one_answer_however_the_filter_is_written(runtime, gold, condition):
    clause, gold_condition = CONDITIONS[condition]
    expected = gold(f"SELECT COUNT(*) FROM orders AS o WHERE {gold_condition}")[None]

    as_where = _rows(runtime, _query(ORDER_COUNT, where=[clause]))[None]
    measure_filter = {"kind": "aggregate", "measure": ORDER_COUNT, "filter": {"all": [clause]}}
    as_measure_filter = _rows(runtime, _query(measure_filter))[None]
    as_segment = runtime.segment_preview(f"segment.geo.{condition}")["member_count"]

    assert as_where == as_measure_filter == as_segment == expected
    entity = "region" if clause["field"] == REGION else "country"
    column = _column(entity, f"{entity}_name")
    if clause["op"] == "missing":
        # A few reads still join a lookup inner (on ClickHouse, inside a metric predicate's own
        # query, beside a time role), so a condition a row with no match could satisfy is
        # refused, never answered differently by where it is read.
        with pytest.raises(SemanticLayerError) as raised:
            _rows(runtime, _query(_count_if(column, "IS", None)))
        assert raised.value.details["reason"] == "null_accepting_condition"
    else:
        as_aggregate_if = _count_if(column, "=", clause["value"])
        assert _rows(runtime, _query(as_aggregate_if))[None] == expected


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


def _orders_with(**minimums: int) -> dict[str, Any]:
    """The population of the orders whose each measure is at least its minimum."""
    predicate = {"entity": "entity.geo_order", "op": "gte", "over": "all_time"}
    predicates = [
        {**predicate, "measure": f"measure.geo.{name}", "value": minimum}
        for name, minimum in minimums.items()
    ]
    return {
        "kind": "scoped_aggregate",
        "measure": "measure.geo.order_population",
        "aggregation": "count_distinct",
        "predicates": predicates,
    }


# Of each country's orders, the share of at least 15: a ratio of two scoped populations,
# which lowers to a snapshot of the orders joined to the sets they belong to.
SHARE_OF_BIG_ORDERS = {
    "kind": "ratio",
    "numerator": _orders_with(order_count=1, amount=15),
    "denominator": _orders_with(order_count=1),
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
        _query("measure.geo.amount", group_by=[COUNTRY]),
        "_source_rollup",
        f"SELECT {SQL_COUNTRY}, SUM(o.amount) FROM orders AS o GROUP BY 1",
        _query("measure.geo.amount"),
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


_joins_for_paths = sql_lowering._joins_for_paths


def _inner_lookups(*args: Any, **kwargs: Any) -> list[SqlJoin]:
    joins = _joins_for_paths(*args, **kwargs)
    return [dataclasses.replace(join, join_type="INNER") for join in joins]


@pytest.mark.parametrize("name", list(LEAVES))
def test_every_leaf_keeps_the_rows(runtime, rollup_safe_runtime, gold, monkeypatch, tmp_path, name):
    """ClickHouse reads '' or 0, not NULL, from an unmatched outer-join column, so its lookups
    stay inner in every leaf. The check catches a leaf that bypasses the rule: with every
    lookup joined INNER, it reports the joins and the lost rows."""
    leaf = LEAVES[name]
    leaf_runtime = rollup_safe_runtime if leaf.rollup_safe else runtime
    package = dataclasses.replace(leaf_runtime.config.package, warehouse="clickhouse")
    clickhouse = dataclasses.replace(leaf_runtime.config, package=package)

    assert _leaf_violations(leaf_runtime, gold, leaf) == []
    joins = _lookup_joins(_sql(clickhouse, leaf.query))
    assert joins and all(kind == "INNER" for kind, _ in joins)

    for module in (leaves, lowering_common, plan_checks, predicate_sets):
        monkeypatch.setattr(module, "_joins_for_paths", _inner_lookups)
    inner = Runtime.from_path(str(_write_package(tmp_path, rollup_safe=leaf.rollup_safe)))
    try:
        violations = _leaf_violations(inner, gold, leaf)
    finally:
        inner.close()
    assert any(text.startswith("lookup joins") for text in violations)
    assert any("is not gold" in text for text in violations)


# Per filter: its op and value, and the same condition on {region} in gold SQL.
OPERATORS = {
    "is": ("is", "North", "{region} = 'North'"),
    "not": ("not", "North", "{region} IS DISTINCT FROM 'North'"),
    "in": ("is", ["North", "East"], "{region} IN ('North', 'East')"),
    "not_in": ("not", ["North", "East"], "({region} IS NULL OR {region} NOT IN ('North', 'East'))"),
    "missing": ("missing", True, "{region} IS NULL"),
}
SQL_ORDERS_BY_TYPE_WHERE = (
    "SELECT i.item_type, COUNT(DISTINCT o.order_id) FROM orders AS o JOIN items AS i"
    " ON i.order_id = o.order_id WHERE {condition} GROUP BY 1"
)
# Per leaf: the query without the region filter, and the gold with a {condition} to fill in.
FILTERED_LEAVES = {
    "root": (_query(ORDER_COUNT), "SELECT COUNT(*) FROM orders AS o WHERE {condition}", False),
    "de_duplicated": (_query(ORDER_COUNT, group_by=[TYPE]), SQL_ORDERS_BY_TYPE_WHERE, False),
    "entity_in_terms_of": (_query(ORDER_COUNT, group_by=[TYPE]), SQL_ORDERS_BY_TYPE_WHERE, True),
    "exists": (
        _query(ORDER_COUNT, where=[BEVERAGE]),
        "SELECT COUNT(*) FROM orders AS o WHERE {condition} AND EXISTS (SELECT 1 FROM items"
        " AS i WHERE i.order_id = o.order_id AND i.item_type = 'beverage')",
        False,
    ),
}


@pytest.mark.parametrize(
    ("name", "operator"),
    [
        # The root leaf's `is` and `missing` are where filters of the answer test above.
        ("root", "not"),
        ("root", "in"),
        *[(name, op) for name in list(FILTERED_LEAVES)[1:] for op in ("is", "not", "missing")],
        ("exists", "not_in"),
    ],
)
def test_filters_on_a_looked_up_field_follow_the_null_rules_in_each_leaf(
    runtime, rollup_safe_runtime, gold, name, operator
):
    """`missing` selects the rows the region lookup found no match for, as it does a NULL
    region (orders 6, 7, 8, 10 and 11); `not` keeps them and `is` never matches them."""
    query, gold_sql, rollup_safe = FILTERED_LEAVES[name]
    op, value, sql = OPERATORS[operator]
    clause = {"field": REGION, "op": op, "value": value}
    filtered = {**query, "where": [*query.get("where", []), clause]}

    got = _rows(rollup_safe_runtime if rollup_safe else runtime, filtered)

    expected = gold(gold_sql.format(condition=sql.format(region=SQL_REGION)))
    assert got == pytest.approx({key: value for key, value in expected.items() if value})


def test_a_dimension_only_query_lists_the_rows_with_no_match(runtime, gold):
    rows = runtime.query({"group_by": [SEGMENT, COUNTRY]})["rows"]

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
            "input": "measure.geo.amount",
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
    query = _query(_distribution(function), group_by=[SEGMENT])
    query["select"].append("measure.geo.amount")
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
    inner, as a time role read through a lookup does in every leaf, and the rest keep rows.
    Counted from the items, item 111 (order 999, no record) is none of the orders."""
    query = _query(ORDER_COUNT, group_by=[TYPE, COUNTRY], time=MONTHLY)

    sql = _sql(rollup_safe_runtime.config, query)

    assert "FROM items" in sql
    assert re.findall(r"\b(\w+) JOIN (orders|customers|regions|countries)\b", sql) == [
        ("INNER", "orders"),
        *[("LEFT", table) for table in HOPS[COUNTRY]],
    ]
    # Every order is in January: no order is lost.
    assert _rows(rollup_safe_runtime, query) == ORDERS_BY_TYPE_AND_COUNTRY


# The orders that hold an item, by a {group} of the item.
SQL_ORDERS_BY_ITEM = (
    "SELECT {group}, COUNT(DISTINCT o.order_id) FROM orders AS o"
    " JOIN items AS i ON i.order_id = o.order_id GROUP BY 1"
)
SQL_ITEM_CATEGORY = "(SELECT p.category FROM products AS p WHERE p.sku = i.sku)"
# Hot: orders 1, 6, 9 and 10. Cold: 2 and 11. NULL: 3 (no product), 4 (no record) and 5 (melt).
ORDERS_BY_CATEGORY = {"hot": 4, "cold": 2, None: 3}


def _answer(package: Path, query: dict[str, Any], reference: str) -> tuple[str, dict, dict]:
    """The query's SQL and rows on a package of its own, and the reference SQL's on its seed."""
    runtime = Runtime.from_path(str(package))
    try:
        sql, rows = _sql(runtime.config, query), _rows(runtime, query)
    finally:
        runtime.close()
    with duckdb.connect() as connection:
        connection.execute((package / "data" / "seed.sql").read_text())
        gold_rows = connection.execute(reference).fetchall()
    return sql, rows, {row[:-1] if len(row) > 2 else row[0]: row[-1] for row in gold_rows}


# Every pair of (product lookup of the orphan item, time axis, leaf) appears once.
@pytest.mark.parametrize(
    ("sku", "with_time", "rollup_safe"),
    [
        ("NULL", False, False),
        ("NULL", True, True),
        ("'missing'", True, False),
        ("'missing'", False, True),
        ("'coffee'", False, False),
        ("'coffee'", True, True),
    ],
)
def test_a_child_lookup_grouping_counts_only_existing_parents(
    tmp_path, sku, with_time, rollup_safe
):
    """Both leaves count existing orders, even when an orphan item's product lookup finds no
    match. Reading the order's time axis preserves that count and the valid NULL group."""
    seed = f"INSERT INTO items VALUES (111, 999, {sku}, 'beverage');\n"
    sql, rows, reference = _answer(
        _write_package(tmp_path, rollup_safe=rollup_safe, extra_seed=seed),
        _query(ORDER_COUNT, group_by=[CATEGORY], **({"time": MONTHLY} if with_time else {})),
        SQL_ORDERS_BY_ITEM.format(group=SQL_ITEM_CATEGORY),
    )

    assert rows == reference == ORDERS_BY_CATEGORY
    assert ("FROM items" if rollup_safe else "_entity_rows") in sql


# Appended to the rollup-safe graph: a second relationship from an item to its order, and the
# route the order count walks to a product.
OTHER_ORDER = """
    items_other_order:
      as: relationship.items_other_order
      entities: [item, order]
      via: other_order_id
      cardinality: many_to_one
  path_preferences:
    - source_entity: order
      target_entity: product
      relationship_path: [relationship.items_order, relationship.items_product]
"""
OTHER_ORDER_SEED = (
    "ALTER TABLE items ADD COLUMN other_order_id INTEGER;\n"
    "UPDATE items SET other_order_id = order_id;\n"
)
# A row holds wherever a route walks its pair, so a row for item -> order must name the
# relationship the order -> product row walks; naming the other one fails to load.
PIN_PARENT = """
    - source_entity: item
      target_entity: order
      relationship_path: [relationship.items_order]
"""
# The items' relationship to their order, walked from the order only.
REVERSE_ONLY = "      allowed_directions: [reverse]\n"


@pytest.mark.parametrize(
    ("graph", "extra_seed", "dimension", "expected"),
    [
        # An alternative parent relationship never changes the pinned order count, with or
        # without a row for the counted one.
        (
            OTHER_ORDER,
            OTHER_ORDER_SEED + "INSERT INTO items VALUES (111, 999, 'coffee', 'beverage', 1);\n",
            CATEGORY,
            ORDERS_BY_CATEGORY,
        ),
        (
            OTHER_ORDER + PIN_PARENT,
            OTHER_ORDER_SEED + "UPDATE items SET other_order_id = NULL WHERE order_id = 1;\n",
            CATEGORY,
            ORDERS_BY_CATEGORY,
        ),
        (
            OTHER_ORDER + PIN_PARENT,
            OTHER_ORDER_SEED + "DELETE FROM items;\n"
            "INSERT INTO items VALUES (111, 1, 'coffee', 'beverage', 999);\n",
            CATEGORY,
            {"hot": 1},
        ),
        # A supported parent-to-child grouping needs no prohibited child-to-parent lookup.
        (REVERSE_ONLY, ORPHAN_ITEM_SEED, TYPE, {"beverage": 5, "food": 4}),
        (REVERSE_ONLY, ORPHAN_ITEM_SEED, CATEGORY, ORDERS_BY_CATEGORY),
    ],
    ids=[
        "orphan_with_another_existing_parent",
        "valid_parent_with_null_other_parent",
        "valid_parent_with_missing_other_parent",
        "reverse_only_child",
        "reverse_only_child_lookup",
    ],
)
def test_the_orders_own_leaf_counts_them_when_the_child_anchor_declines(
    tmp_path, graph, extra_seed, dimension, expected
):
    group = "i.item_type" if dimension == TYPE else SQL_ITEM_CATEGORY
    sql, rows, reference = _answer(
        _write_package(tmp_path, rollup_safe=True, extra_seed=extra_seed, graph=graph),
        _query(ORDER_COUNT, group_by=[dimension]),
        SQL_ORDERS_BY_ITEM.format(group=group),
    )

    assert rows == reference == expected
    assert "FROM orders" in sql
    assert "FROM items" not in sql
    assert "other_order_id" not in sql


def _refuses_the_child_anchor(call: Any) -> None:
    """``call`` refuses to answer the order count from the items' rows."""
    with pytest.raises(SemanticLayerError) as raised:
        call()
    error = raised.value
    assert error.code == "REWRITE_NOT_SUPPORTED"
    assert error.details["measure_entity"] == "entity.geo_order"
    assert error.details["source_entity"] == "entity.geo_item"


@pytest.mark.parametrize("bypass", ["missing", "left"])
def test_a_child_anchored_count_refuses_a_bypassed_parent_check(
    rollup_safe_runtime, monkeypatch, bypass
):
    """The shared join builder refuses the leaf if its parent check is absent or nullable."""
    if bypass == "missing":
        anchor_plan = sql_lowering._entity_in_terms_of_anchor_plan

        def without_parent_check(*args):
            result = anchor_plan(*args)
            assert result is not None
            result["path_selections"] = [
                row for row in result["path_selections"] if row.purpose != "entity_in_terms_of_root"
            ]
            return result

        for module in (leaves, lowering_common, physical_plan, sql_lowering):
            monkeypatch.setattr(module, "_entity_in_terms_of_anchor_plan", without_parent_check)
    else:
        monkeypatch.setattr(
            paths,
            "_INNER_LOOKUP_PURPOSES",
            paths._INNER_LOOKUP_PURPOSES - {"entity_in_terms_of_root"},
        )

    _refuses_the_child_anchor(
        lambda: _sql(rollup_safe_runtime.config, _query(ORDER_COUNT, group_by=[CATEGORY]))
    )


@pytest.mark.parametrize("relationship", ["items_order", "items_other_order"])
def test_a_child_anchor_guard_refuses_two_parent_relationships(tmp_path, relationship):
    """Bypassing the anchor's decline cannot pass the guard just by joining orders INNER."""
    package = _write_package(
        tmp_path, rollup_safe=True, extra_seed=OTHER_ORDER_SEED, graph=OTHER_ORDER
    )
    root = PathSelection(
        target_entity="entity.geo_order",
        purpose="entity_in_terms_of_root",
        chosen_path=[f"relationship.{relationship}"],
        candidate_paths=[],
        analysis={},
    )
    config = load_package_config(str(package))

    _refuses_the_child_anchor(
        lambda: paths._joins_for_paths(
            "entity.geo_item", [root], config, measure_entity="entity.geo_order"
        )
    )


def _with_rollup(config: Any, source_entity: str) -> Any:
    rollup = AggregateRelationConfig(
        id="aggregate_relation.geo_by_country",
        relation="by_country",
        source_entity=source_entity,
        dimensions=[COUNTRY],
    )
    return dataclasses.replace(config, aggregate_relations=[rollup])


def test_the_entity_in_terms_of_leaf_leaves_a_rollup_dimension_to_the_order_s_leaf(
    rollup_safe_runtime, gold, monkeypatch
):
    """A rollup of the orders holds the country, so the base answers as that rollup does: every
    hop to the country is INNER. Orders 4, 5 and 11 reach no country record and drop out; order
    3's country has no name. Counted from the items, the leaf would not see the orders' rollup,
    so the orders' own leaf answers; with that decline bypassed, the joins refuse."""
    config = _with_rollup(rollup_safe_runtime.config, "entity.geo_order")
    query = _query(ORDER_COUNT, group_by=[TYPE, COUNTRY])

    sql = _sql(config, query)

    assert "FROM orders" in sql
    assert _lookup_joins(sql) == [("INNER", table) for table in HOPS[COUNTRY]]
    expected = gold(
        "SELECT i.item_type, k.country_name, COUNT(DISTINCT o.order_id) FROM orders AS o"
        " JOIN items AS i ON i.order_id = o.order_id"
        " JOIN customers AS c ON c.customer_id = o.customer_id"
        " JOIN regions AS r ON r.region_id = c.region_id"
        " JOIN countries AS k ON k.country_id = r.country_id GROUP BY 1, 2"
    )
    assert gold(sql) == expected
    assert expected == {("beverage", "Atlantis"): 3, ("beverage", None): 1, ("food", "Atlantis"): 1}
    monkeypatch.setattr(lowering_common, "rollup_held_lookups", lambda *args: set())
    _refuses_the_child_anchor(lambda: _sql(config, query))


@pytest.mark.parametrize(
    ("rollup_safe", "marker"),
    [(False, "_entity_rows"), (True, "FROM items")],
    ids=["de_duplicated", "entity_in_terms_of"],
)
def test_a_rollup_of_another_model_changes_no_leaf(
    runtime, rollup_safe_runtime, gold, rollup_safe, marker
):
    """A rollup of the items that holds the country belongs to item measures: the order count
    joins its lookups as the base tables do from either leaf, with both NULL groups."""
    config = _with_rollup(
        (rollup_safe_runtime if rollup_safe else runtime).config, "entity.geo_item"
    )

    sql = _sql(config, _query(ORDER_COUNT, group_by=[TYPE, COUNTRY]))

    assert marker in sql
    assert _lookup_joins(sql) == [("LEFT", table) for table in HOPS[COUNTRY]]
    assert gold(sql) == gold(SQL_ORDERS_BY_TYPE_AND_COUNTRY) == ORDERS_BY_TYPE_AND_COUNTRY


def test_a_dimension_only_query_reads_no_rollup(runtime, gold):
    """No measure is read, so no rollup applies: the items rollup holding the country leaves
    the listing's NULL country for food (items 105, 106 and 109) in."""
    config = _with_rollup(runtime.config, "entity.geo_item")

    sql = _sql(config, {"version": 1, "group_by": [TYPE, COUNTRY]})

    assert "FROM items" in sql
    assert _lookup_joins(sql) == [("LEFT", table) for table in HOPS[COUNTRY]]
    item_country = (
        "(SELECT k.country_name FROM orders AS o, customers AS c, regions AS r, countries AS k"
        " WHERE o.order_id = i.order_id AND c.customer_id = o.customer_id"
        " AND r.region_id = c.region_id AND k.country_id = r.country_id)"
    )
    expected = gold(f"SELECT DISTINCT i.item_type, {item_country}, 1 FROM items AS i")
    assert gold(f"SELECT *, 1 FROM ({sql})") == expected
    assert ("food", None) in expected


def test_a_lookup_after_a_one_to_many_hop_joins_left_inside_exists(runtime, gold):
    """A child filter reads the item's product: the lookup joins LEFT inside the EXISTS that
    reads the items, not as an EXISTS of its own."""
    query = _query(ORDER_COUNT, where=[{"field": CATEGORY, "op": "=", "value": "hot"}])

    sql = _sql(runtime.config, query)

    assert (
        "EXISTS ( SELECT 1 AS match FROM items LEFT JOIN products ON items.sku = products.sku"
        " WHERE orders.order_id = items.order_id AND products.category = 'hot' )"
    ) in sql
    assert _rows(runtime, query) == gold(
        "SELECT COUNT(*) FROM orders AS o WHERE EXISTS (SELECT 1 FROM items AS i WHERE"
        " i.order_id = o.order_id AND (SELECT p.category FROM products AS p"
        " WHERE p.sku = i.sku) = 'hot')"
    )


def _callers(function: str) -> set[str]:
    """``module:function`` for every function in the engine that calls ``function``."""
    root = Path(__file__).resolve().parents[2] / "semantic_rails"
    return {
        f"{path.stem}:{node.name}"
        for path in root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == function
            for call in ast.walk(node)
        )
    }


def test_one_place_decides_how_a_relationship_joins():
    """Every leaf joins its paths through ``_joins_for_paths``, the one caller of the join
    condition builder, so no leaf can choose a lookup's join type on its own; only a metric
    predicate's own query asks it to join lookups INNER (a distribution's per-entity values
    keep their rows, as the call grouped by the entity does); and only it reads which lookups a
    rollup holds, for the rollup's own model."""
    assert _callers("_join_on_for_relationship") == {"paths:_joins_for_paths"}
    assert _callers("inner_lookups") == {"compiler:_compile_predicate_source_ast"}
    assert _callers("rollup_dimension_entities") == {
        "paths:_joins_for_paths",
        "paths:rollup_held_lookups",
    }
