"""Measures across one-to-many hops count each row of their own entity once.

An order-grain measure filtered or grouped by an item dimension (orders -> order_items) used to
be refused with MIXED_GRAIN_INVALID. The leaf now keeps one row per (order key, output grain)
before it aggregates: filtered, an order counts once at all (EXISTS); grouped, it counts once,
with its whole value, in every product type it contains ("orders that included it"): a distinct
count, a sum, a count or an average, the groups overlapping. Every answer is checked on DuckDB
against reference SQL written independently of the engine.

The item -> order relationship here is inferred, with no ``rollup_safe`` opt-in, so the
entity_in_terms_of shortcut does not apply.
"""

from __future__ import annotations

from collections.abc import Iterator
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
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.hidden_absent import (
    CALLER,
    absent,
    envelope,
    visibility_policy,
    with_policies,
)
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
  seed: {kind: sql_script, source: data/seed.sql}}
"""
# Each model's id and relation are its name.
MODELS = """
orders:
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
    order_rows: {label: Stored orders, kind: aggregate, expr: {kind: literal, value: 1},
      accumulation: {kind: flow}, value_type: count}
order_items:
  entities: {item: {}, order: {}, product: {expr: sku}}
  dimensions:
    product_type: {label: Item product type, kind: categorical}
    is_hot: {label: Hot item, kind: boolean}
  measures:
    item_revenue: {label: Item revenue, kind: aggregate, expr: revenue,
      accumulation: {kind: flow}, value_type: currency, times: [temporal_role.hop_order_ordered_at]}
products:
  entities: {product: {}}
  dimensions: {category: {label: Category, kind: categorical}}
customers:
  entities: {customer: {}}
  dimensions: {segment: {label: Customer segment, kind: categorical}}
  measures:
    customer_count: {label: Customers, kind: entity_count, entity_key: customer_id,
      accumulation: {kind: event}, value_type: count}
    credit: {label: Credit, kind: aggregate, expr: credit, accumulation: {kind: flow},
      value_type: currency}
    customer_rows: {label: Customer records, kind: aggregate, expr: {kind: literal, value: 1},
      accumulation: {kind: flow}, value_type: count}
    score: {label: Score, kind: aggregate, expr: score, accumulation: {kind: flow},
      additive: false}
sessions:
  entities: {session: {}, customer: {}}
  dimensions: {channel: {label: Channel, kind: categorical}}
payments:
  entities: {receipt: {}, order: {}}
  measures:
    paid: {label: Paid, kind: aggregate, expr: amount, accumulation: {kind: flow},
      value_type: currency}
order_payments:
  entities: {payment: {}, order: {}}
  dimensions: {method: {label: Payment method, kind: categorical}}
coupons:
  entities: {coupon: {}}
  measures:
    face_value: {label: Face value, kind: aggregate, expr: face_value,
      accumulation: {kind: flow}, value_type: currency}
"""
ENTITIES = {
    "order": ["order_id", "orders"],
    "item": ["item_id", "order_items"],
    "product": ["sku", "products"],
    "customer": ["customer_id", "customers"],
    "session": ["session_id", "sessions"],
    "coupon": ["coupon_id", "coupons"],
    "payment": ["payment_id", "order_payments"],
    # Each payment of an order is a receipt, a child of its order.
    "receipt": [["order_id", "payment_id"], "payments"],
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


def _is(field: str, value: Any, op: str = "=") -> dict[str, Any]:
    return {"field": field, "op": op, "value": value}


BEVERAGE = _is(TYPE, "beverage")
WEB = _is(CHANNEL, "web")
HOT_ITEM = _is(HOT, True)
COUPON_1 = _is(COUPON, 1)
MONTHLY = {"temporal_role": ROLE, "grain": "month"}
ORDER = "entity.hop_order"
ITEM = "entity.hop_item"


def _measure(name: str, aggregation: str = "", alias: str = "") -> dict[str, Any]:
    expression = {"kind": "measure", "measure": f"measure.hop.{name}"}
    if aggregation:
        expression["aggregation"] = aggregation
    return {"expression": expression, "as": alias or f"{name}_{aggregation}".rstrip("_")}


def _bound(
    clause: dict[str, Any], measure: str = "revenue", aggregation: str = "sum", alias: str = ""
) -> dict[str, Any]:
    """A measure with its own filter (the metric_filter path), under a name of its own: the
    measure's name is the governed, unfiltered measure's."""
    expression = {
        "kind": "aggregate",
        "measure": f"measure.hop.{measure}",
        "aggregation": aggregation,
        "filter": {"all": [clause]},
    }
    return {"expression": expression, "as": alias or f"{measure}_filtered"}


def _ask(measure: str = "revenue", aggregation: str = "", **query: Any) -> dict[str, Any]:
    return {"select": [_measure(measure, aggregation)], **query}


def _where(op: str, value: Any, field: str = TYPE) -> dict[str, Any]:
    return _ask(where=[_is(field, value, op)])


def _bound_filter(clause: dict[str, Any]) -> dict[str, Any]:
    return {"select": [_bound(clause)]}


def _conditional(expression: dict[str, Any], **query: Any) -> dict[str, Any]:
    return {"select": [{"expression": expression, "as": "v"}], **query}


def _compare(column: str, op: str, value: Any, entity: str = ORDER) -> dict[str, Any]:
    left = {"kind": "column", "entity": entity, "column": column}
    right = {"kind": "literal", "value": value}
    return {"kind": "comparison", "op": op, "left": left, "right": right}


def _if(
    aggregation: str, condition: dict[str, Any], value: str = "", entity: str = ORDER
) -> dict[str, Any]:
    """An aggregate_if over the rows of ``entity``; without ``value`` it counts them."""
    expression = {"kind": "aggregate_if", "aggregation": aggregation, "condition": condition}
    if value:
        expression["value"] = {"kind": "column", "entity": entity, "column": value}
    return expression


def _ratio(numerator: dict[str, Any], denominator: Any) -> dict[str, Any]:
    return {"kind": "ratio", "numerator": numerator, "denominator": denominator}


FILTERED_SUM = _ask(where=[BEVERAGE])
GROUPED_COUNT = {"select": [_measure("order_count", alias="orders")], "group_by": [TYPE]}
# Customer 10's orders, and their revenue: the conditional forms of order_count and revenue.
OWN_ORDERS_IF = _if("count_distinct", _compare("customer_id", "=", 10), "order_id")
OWN_REVENUE_IF = _if("sum", _compare("customer_id", "=", 10), "total")
OWN_SHARE = _ratio(OWN_ORDERS_IF, {"kind": "measure", "measure": "measure.hop.order_count"})
# Each payment is a receipt; the paid measure's rows are receipts.
PAID_IF = _if(
    "sum", _compare("amount", ">", 0, "entity.hop_receipt"), "amount", "entity.hop_receipt"
)


# The reference SQL's building blocks.
def _has_item(condition: str, order: str = "o.order_id") -> str:
    return f"EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = {order} AND {condition})"


def _has_session(channel: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM sessions s "
        f"WHERE s.customer_id = o.customer_id AND s.channel = '{channel}')"
    )


HAS_BEVERAGE = _has_item("i.product_type = 'beverage'")
HAS_JAFFLE = _has_item("i.product_type = 'jaffle'")
HAS_WEB_SESSION = _has_session("web")
ORDERS_SUM = "SELECT SUM(o.total) FROM orders o WHERE {}"
ORDERS_COUNT = "SELECT COUNT(*) FROM orders o WHERE {}"
OWN_ORDERS_SQL = "COUNT(DISTINCT CASE WHEN o.customer_id = 10 THEN o.order_id END)"
OWN_REVENUE_SQL = "SUM(CASE WHEN o.customer_id = 10 THEN o.total END)"


# One runtime per package, opened by its module fixture and shared by the queries on it.
_RUNTIMES: dict[Path, Runtime] = {}


def _serve(root: Path) -> Iterator[Path]:
    """Open the package's runtime (seeding its warehouse) for the module, then close it."""
    _RUNTIMES[root] = opened(Runtime.from_path(str(root)))
    yield root
    _RUNTIMES.pop(root).close()


def _write(
    root: Path,
    models: dict[str, Any],
    entities: dict[str, list[Any]],
    relationships: dict[str, Any] = RELATIONSHIPS,
    seed: str = SEED,
) -> Path:
    """Write a package named after ``root``; each model's id and relation are its name."""
    keys = {
        name: {"label": name, "key": key if isinstance(key, list) else [key], "model": model}
        for name, (key, model) in entities.items()
    }
    files = {
        "package.yml": PACKAGE.replace("hop", root.name),
        "data/seed.sql": seed,
        "graph.yml": yaml.safe_dump({"graph": {"entities": keys, "relationships": relationships}}),
        **{
            f"models/{name}.yml": yaml.safe_dump({"model": {"id": name, "relation": name, **doc}})
            for name, doc in models.items()
        },
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    yield from _serve(
        _write(tmp_path_factory.mktemp("hop") / "hop", yaml.safe_load(MODELS), ENTITIES)
    )


@pytest.fixture(scope="module")
def receipts_by_order(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Receipts keyed by their order, though each payment is a row, and no measure on them:
    nothing in the package says a receipt holds more than one row."""
    models = yaml.safe_load(MODELS)
    models["order_items"]["entities"]["receipt"] = {"expr": "order_id"}
    models["payments"] = {"entities": {"receipt": {}}}
    entities = {**ENTITIES, "receipt": ["order_id", "payments"]}
    yield from _serve(_write(tmp_path_factory.mktemp("receipts") / "hop", models, entities))


def _run(package: Path, query: dict[str, Any], *, validate: bool = False) -> dict[str, Any]:
    engine = _RUNTIMES[package]
    # The references are plain SQL, where a sum of no rows is NULL, as under the query scope;
    # test_observation_scope covers the dataset scope on these leaves.
    payload = {"observation_scope": "query", **query}
    return engine.validate(payload) if validate else engine.query(payload)


def _answer(package: Path, query: dict[str, Any]) -> tuple[list[tuple[Any, ...]], str]:
    """The answer's rows and the SQL it ran."""
    result = _run(package, query)
    assert result.get("ok", True), result
    return _normal(tuple(row.values()) for row in typed_rows(result)), result["rendered_sql"]


def _rows(package: Path, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    return _answer(package, query)[0]


def _reference(package: Path, sql: str, params: Any = ()) -> list[tuple[Any, ...]]:
    """SQL run on the package's warehouse: a reference, or SQL the engine compiled."""
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        return _normal(conn.execute(sql, params).fetchall())


def _config(package: Path, warehouse: str = "duckdb") -> Any:
    config = load_package_config(str(package))
    return replace(config, package=replace(config.package, warehouse=warehouse))


def _edited(package: Path, field: str, object_id: str, **changes: Any) -> Any:
    """The package's config with one relationship or measure changed in memory."""
    config = load_package_config(str(package))
    rows = [
        replace(row, **changes) if row.id == object_id else row for row in getattr(config, field)
    ]
    return replace(config, **{field: rows})


def _compile(config: Any, query: dict[str, Any]) -> dict[str, Any]:
    return compile_query(config, Registry(config), {"version": 1, **query})


def _compiled_rows(package: Path, config: Any, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    return _reference(package, _compile(config, query)["prepared_query"].sql)


def _compile_refusal(config: Any, query: dict[str, Any], code: str) -> dict[str, Any]:
    """The refusal's details, once its code is checked."""
    with pytest.raises(SemanticLayerError) as caught:
        _compile(config, query)
    assert caught.value.code == code
    return caught.value.details


def _clickhouse(package: Path, query: dict[str, Any]) -> str:
    """ClickHouse's SQL for ``query``, which DuckDB runs without the trailing setting."""
    sql = _compile(_config(package, "clickhouse"), query)["prepared_query"].sql
    return sql.removesuffix("\nSETTINGS join_use_nulls = 1")


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
    reference = ORDERS_SUM.format(f"{IN_Q4} AND {HAS_BEVERAGE}")
    rows = _rows(package, {**FILTERED_SUM, "time": Q4_2016})
    assert rows == _reference(package, reference) == [(30.0,)]
    naive = f"""
        SELECT SUM(o.total) FROM orders o JOIN order_items i ON i.order_id = o.order_id
        WHERE {IN_Q4} AND i.product_type = 'beverage'
    """
    assert _reference(package, naive) == [(40.0,)]  # what a plain join would have said


@pytest.mark.parametrize(
    ("measure", "field", "value", "reference", "expected"),
    [
        ("order_count", TYPE, "beverage", ORDERS_COUNT.format(HAS_BEVERAGE), 4),
        ("revenue", TYPE, "beverage", ORDERS_SUM.format(HAS_BEVERAGE), 70),
        ("order_count", CHANNEL, "web", ORDERS_COUNT.format(HAS_WEB_SESSION), 4),
        ("revenue", CHANNEL, "web", ORDERS_SUM.format(HAS_WEB_SESSION), 60),
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
    query = _ask(measure, where=[_is(field, value, "is")])
    rows, sql = _answer(package, query)
    assert rows == _reference(package, reference) == [(expected,)]
    assert "EXISTS (" in sql
    assert "SELECT DISTINCT" not in sql
    assert "JOIN" not in sql


def test_measure_bound_child_filter_does_not_change_unfiltered_sibling(package: Path) -> None:
    query = {
        "select": [_bound(_is(CHANNEL, "app"), alias="matching"), _measure("revenue", alias="all")]
    }
    reference = (
        f"SELECT ({ORDERS_SUM.format(_has_session('app'))}), (SELECT SUM(total) FROM orders)"
    )
    assert _rows(package, query) == _reference(package, reference) == [(30.0, 290.0)]


HOT_BEVERAGE = "i.product_type = 'beverage' AND i.is_hot"


@pytest.mark.parametrize(
    ("query", "negation", "condition"),
    [
        (FILTERED_SUM, "", "i.product_type = 'beverage'"),
        (_bound_filter(BEVERAGE), "", "i.product_type = 'beverage'"),
        # A child group lowers in the same semi-join leaf as a flat child filter.
        (_ask(where=[{"entity": ITEM, "any": [BEVERAGE, HOT_ITEM]}]), "", HOT_BEVERAGE),
        (_ask(where=[{"entity": ITEM, "none": [BEVERAGE, HOT_ITEM]}]), "NOT ", HOT_BEVERAGE),
    ],
    ids=["query_filter", "measure_filter", "any_group", "none_group"],
)
def test_time_bounded_child_filters_keep_window_observation(
    package: Path, query: dict[str, Any], negation: str, condition: str
) -> None:
    window = {"temporal_role": ROLE, "grain": "month", "start": "2016-10-01", "end": "2017-04-01"}
    # The named window answers each of its months: 0 where none matched.
    reference = f"""
        WITH matching AS (
          SELECT DATE_TRUNC('month', o.ordered_at) AS month, SUM(o.total) AS revenue
          FROM orders o
          WHERE o.ordered_at >= TIMESTAMP '2016-10-01' AND o.ordered_at < TIMESTAMP '2017-04-01'
            AND {negation}{_has_item(condition)}
          GROUP BY month
        )
        SELECT months.month, COALESCE(matching.revenue, 0)
        FROM GENERATE_SERIES(TIMESTAMP '2016-10-01', TIMESTAMP '2017-03-01',
                             INTERVAL '1 month') AS months(month)
        LEFT JOIN matching USING (month)
    """
    rows, sql = _answer(package, {**query, "time": window})
    assert rows == _reference(package, reference)
    assert f"{negation}EXISTS (" in sql and "SELECT DISTINCT" not in sql
    # Rewritten fanout leaves retain observation inside the window, including filled buckets.
    assert "coverage_" not in sql and sql.count("FROM orders") == 1


def test_child_filter_correlates_every_authored_join_column(package: Path) -> None:
    config = _edited(
        package,
        "relationships",
        "relationship.order_items_order",
        source_columns=["order_id", "revenue"],
        target_columns=["order_id", "total"],
    )
    reference = ORDERS_SUM.format(_has_item("i.revenue = o.total AND i.product_type = 'beverage'"))
    assert (
        _compiled_rows(package, config, FILTERED_SUM) == _reference(package, reference) == [(40.0,)]
    )


def test_a_child_value_authored_at_parent_grain_still_refuses(package: Path) -> None:
    item_revenue = ColumnRefExpr(entity=ITEM, column="revenue")
    config = _edited(package, "measures", "measure.hop.revenue", expr=item_revenue)
    assert (
        _compile_refusal(config, FILTERED_SUM, "MIXED_GRAIN_INVALID")["purpose"] == "measure_expr"
    )


@pytest.mark.parametrize("cardinality", ["N:N", "unknown"])
def test_child_filter_does_not_admit_many_to_many_or_unknown_hops(
    package: Path, cardinality: str
) -> None:
    config = _edited(
        package,
        "relationships",
        "relationship.order_items_order",
        cardinality=cardinality,
        safety="requires_rewrite",
    )
    _compile_refusal(config, FILTERED_SUM, "MIXED_GRAIN_INVALID")


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ({"kind": "measure", "measure": "measure.hop.order_count"}, 4),
        ({"kind": "measure", "measure": "measure.hop.revenue"}, 70),
        (OWN_REVENUE_IF, 30),
        (OWN_SHARE, 0.5),
    ],
    ids=["parent_count", "parent_sum", "conditional_sum", "conditional_ratio"],
)
def test_clickhouse_uses_equivalent_parent_deduplication(
    package: Path, expression: dict[str, Any], expected: float
) -> None:
    query = _conditional(expression, where=[BEVERAGE])
    sql = _clickhouse(package, query)
    assert _reference(package, sql) == _rows(package, query) == [(expected,)]
    assert "SELECT DISTINCT" in sql
    assert "EXISTS" not in sql


@pytest.mark.parametrize(
    "query",
    [
        _ask("face_value", where=[BEVERAGE]),
        _ask("order_count", where=[WEB]),
        _ask(where=[WEB]),
        _conditional(OWN_REVENUE_IF, where=[WEB]),
        _conditional(OWN_SHARE, where=[WEB]),
        # Beside a lookup: filtered by it, or grouped by it under the measure's own filter.
        _ask("order_count", where=[WEB, COUPON_1]),
        {"select": [_bound(WEB, "order_count", "count_distinct")], "group_by": [COUPON]},
    ],
    ids=[
        "alternate_key",
        "lookup_then_child_count",
        "lookup_then_child_sum",
        "lookup_then_child_conditional_sum",
        "lookup_then_child_conditional_ratio",
        "lookup_then_child_beside_lookup_filter",
        "lookup_then_child_measure_filter_beside_grouped_lookup",
    ],
)
def test_clickhouse_refuses_child_filter_paths_requiring_exists(
    package: Path, query: dict[str, Any]
) -> None:
    clickhouse = _config(package, "clickhouse")
    query = {"observation_scope": "query", **query}
    assert "ClickHouse" in _compile_refusal(clickhouse, query, "MIXED_GRAIN_INVALID")["why_invalid"]


def test_a_conditional_aggregate_of_rows_of_unknown_grain(receipts_by_order: Path) -> None:
    """Nothing says receipts hold a row per payment. EXISTS reads each payment once anyway;
    ClickHouse's one row per receipt would merge order 1's two payments of 5, so it refuses all
    but the aggregations that merging cannot change."""
    package = receipts_by_order
    config = load_package_config(str(package))
    receipt = "entity.hop_receipt"
    payments = _if("count_distinct", _compare("amount", ">", 0, receipt), "payment_id", receipt)
    for expression, value in ((PAID_IF, 10), (payments, 2)):
        query = _conditional(expression, where=[BEVERAGE])
        assert _compiled_rows(package, config, query) == [(value,)]
    reference = PAID_REFERENCE.replace("SUM(amount)", "SUM(p.amount), COUNT(DISTINCT p.payment_id)")
    assert _reference(package, reference) == [(10.0, 2)]
    clickhouse = _config(package, "clickhouse")
    assert "SELECT DISTINCT" in _compile(clickhouse, query)["sql"]
    details = _compile_refusal(
        clickhouse, _conditional(PAID_IF, where=[BEVERAGE]), "MIXED_GRAIN_INVALID"
    )
    assert "could merge rows that share a key" in details["why_invalid"]


@pytest.mark.parametrize(
    ("lookup", "bound_filter"),
    [
        ({"group_by": [COUPON]}, False),
        ({"where": [COUPON_1]}, True),
        ({"where": [{"field": COUPON, "op": "IS NULL", "value": None}]}, False),
        ({"group_by": [COUPON]}, True),
    ],
    ids=[
        "where-grouped_lookup",
        "measure_filter-lookup_filter",
        "where-null_lookup_filter",
        "measure_filter-grouped_lookup",
    ],
)
def test_clickhouse_keeps_supported_lookups_beside_a_child_filter(
    package: Path, lookup: dict[str, Any], bound_filter: bool
) -> None:
    orders = _bound(BEVERAGE, "order_count", "count_distinct", "orders")
    # A plain-SQL reference: the query scope, as in _run.
    query = {"observation_scope": "query", "select": [orders], **lookup}
    if not bound_filter:
        query["select"] = [_measure("order_count")]
        query["where"] = [BEVERAGE, *lookup.get("where", [])]
    sql = _clickhouse(package, query)
    assert "SELECT DISTINCT" in sql
    assert "INNER JOIN coupons" in sql
    assert "EXISTS" not in sql
    coupon_orders = "FROM orders o JOIN coupons c ON c.code = o.coupon_code WHERE"
    if "group_by" in lookup:
        reference = f"""
            SELECT c.coupon_id, COUNT(DISTINCT o.order_id) {coupon_orders} {HAS_BEVERAGE}
            GROUP BY 1
        """
    else:
        coupon = "c.coupon_id IS NULL" if lookup["where"][0]["value"] is None else "c.coupon_id = 1"
        reference = f"""
            SELECT CASE WHEN COUNT(*) > 0 THEN COUNT(DISTINCT o.order_id) ELSE NULL END
            {coupon_orders} {coupon} AND {HAS_BEVERAGE}
        """
    assert _reference(package, sql) == _reference(package, reference)


DIAMOND_MODELS = """
accounts:
  entities: {account: {}}
  measures:
    amount: {label: Amount, kind: aggregate, expr: amount, accumulation: {kind: flow},
      value_type: currency}
clients: {entities: {client: {}}}
branches: {entities: {branch: {}}, dimensions: {name: {label: Name, kind: categorical}}}
zones: {entities: {zone: {}}}
districts: {entities: {district: {}}, dimensions: {category: {label: Category, kind: categorical}}}
"""
DIAMOND_SEED = """
CREATE TABLE accounts AS SELECT * FROM (VALUES (1, 10, 100, 50), (2, 20, 200, 70))
  AS t(account_id, client_id, branch_id, amount);
CREATE TABLE clients AS SELECT * FROM (VALUES (10), (20)) AS t(client_id);
CREATE TABLE branches AS SELECT * FROM (VALUES (100, 'east'), (200, 'west')) AS t(branch_id, name);
CREATE TABLE zones AS SELECT * FROM (VALUES (1000, 100), (2000, 200)) AS t(zone_id, branch_id);
CREATE TABLE districts AS SELECT * FROM (VALUES
  (1, 10, 2000, 'premium'), (2, 20, 1000, 'standard'))
  AS t(district_id, client_id, zone_id, category);
"""
# An account reaches a district through its client or through its branch's zone.
CLIENT_ROUTE = ["relationship.account_client", "relationship.district_client"]
BRANCH_ROUTE = [
    "relationship.account_branch",
    "relationship.zone_branch",
    "relationship.district_zone",
]
PREMIUM_DISTRICT = _is("dimension.diamond_district_category", "premium")


@pytest.fixture(scope="module")
def diamond_package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    edges = {
        "account_client": ("account", "client", "client_id"),
        "district_client": ("district", "client", "client_id"),
        "account_branch": ("account", "branch", "branch_id"),
        "zone_branch": ("zone", "branch", "branch_id"),
        "district_zone": ("district", "zone", "zone_id"),
    }
    relationships = {
        name: {
            "as": f"relationship.{name}",
            "entities": [source, target],
            "cardinality": "many_to_one",
            "via": [column],
            "target": [column],
        }
        for name, (source, target, column) in edges.items()
    }
    entities = {
        entity: [f"{entity}_id", "branches" if entity == "branch" else f"{entity}s"]
        for entity in ("account", "client", "branch", "zone", "district")
    }
    root = tmp_path_factory.mktemp("diamond") / "diamond"
    models = yaml.safe_load(DIAMOND_MODELS)
    yield from _serve(_write(root, models, entities, relationships, DIAMOND_SEED))


def _pinned(config: Any, path: list[str]) -> Any:
    """``config`` with the account -> district route recorded."""
    route = PathPreferenceConfig("entity.diamond_account", "entity.diamond_district", path)
    return replace(config, path_preferences=[route])


def _refused_until_a_route_is_recorded(config: Any, query: dict[str, Any]) -> None:
    """Two routes and neither is the account's own key: refused until the package records one."""
    details = _compile_refusal(config, query, "AMBIGUOUS_PATH")
    assert details["reason"] == "route_decision_required"
    assert details["target"] == "entity.diamond_district"
    options = details["clarification"]["options"]
    assert [option["relationship_path"] for option in options] == [CLIENT_ROUTE, BRANCH_ROUTE]


# Each pair of (route start, filter place, lookup grouping) values appears once.
@pytest.mark.parametrize(
    ("alternate_key", "bound_filter", "beside_lookup"),
    [(False, False, False), (False, True, True), (True, False, True), (True, True, False)],
)
def test_new_child_filter_paths_require_one_candidate_or_a_pin(
    diamond_package: Path, alternate_key: bool, bound_filter: bool, beside_lookup: bool
) -> None:
    expression: dict[str, Any] = {"measure": "measure.diamond.amount"}
    # A filtered amount is not the governed `amount`, so it answers under its own name.
    alias = "premium_amount" if bound_filter else "amount"
    query: dict[str, Any] = {"select": [{"expression": expression, "as": alias}]}
    if bound_filter:
        expression.update(kind="aggregate", aggregation="sum", filter={"all": [PREMIUM_DISTRICT]})
    else:
        query["where"] = [PREMIUM_DISTRICT]
    if beside_lookup:
        query["group_by"] = ["dimension.diamond_branch_name"]
    config = (
        _edited(diamond_package, "relationships", "relationship.account_client", cardinality="1:N")
        if alternate_key
        else load_package_config(str(diamond_package))
    )
    _refused_until_a_route_is_recorded(config, query)
    routes = [
        (
            CLIENT_ROUTE,
            "SELECT SUM(a.amount) FROM accounts a WHERE EXISTS (SELECT 1 FROM districts d "
            "WHERE d.client_id = a.client_id AND d.category = 'premium')",
            50,
            "east",
        ),
        (
            BRANCH_ROUTE,
            "SELECT SUM(a.amount) FROM accounts a WHERE EXISTS (SELECT 1 FROM zones z "
            "JOIN districts d ON d.zone_id = z.zone_id "
            "WHERE z.branch_id = a.branch_id AND d.category = 'premium')",
            70,
            "west",
        ),
    ]
    # Pinning must select the authored route even when it is longer.
    for path, reference, expected, branch in routes:
        compiled = _compile(_pinned(config, path), query)
        [selection] = [
            row
            for row in compiled["logical_plan"].measure_plans[0].path_selections
            if row.target_entity == "entity.diamond_district"
        ]
        assert selection.candidate_paths == [path]
        actual = _reference(diamond_package, compiled["prepared_query"].sql)
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
        "select": [{"expression": expression, "as": "value"}],
        "where": [{**PREMIUM_DISTRICT, "op": "is"}],
        "observation_scope": "query",  # a plain-SQL reference, as in _run
    }
    config = load_package_config(str(diamond_package))
    _refused_until_a_route_is_recorded(config, query)
    # The client route reaches account 1 (50, so no amount over 60), the branch route account 2
    # (70). A sum of nothing reads NULL, and so does a ratio over it.
    for path, expected in [
        (CLIENT_ROUTE, None),
        (BRANCH_ROUTE, 70 if form == "conditional" else 1),
    ]:
        compiled = _compile(_pinned(config, path), query)
        assert "EXISTS (" in compiled["sql"] and "JOIN districts" not in compiled["sql"]
        assert _reference(diamond_package, compiled["prepared_query"].sql) == [(expected,)]


HOT_ITEMS = {"dimension": HOT, "attribute": "hot", "type": "boolean"}
OWN_ORDERS = {"dimension": "dimension.hop_order_customer_id", "attribute": "customer",
              "type": "integer"}  # fmt: skip


def _under_row_policy(
    package: Path, policy: dict[str, Any], attributes: dict[str, Any], query: dict[str, Any]
) -> dict[str, Any]:
    policies = [SemanticPolicyConfig(id="policy.hop.rows", kind="row_filter", config=policy)]
    config = replace(load_package_config(str(package)), semantic_policies=policies)
    context = RequestContext(attributes=attributes).to_policy_context()
    filters = row_filters_for_context(config, context)
    return compile_query(config, Registry(config), {"version": 1, **query}, row_filters=filters)


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
def test_child_filters_under_a_row_policy(
    package: Path, policy: dict[str, Any], attributes: dict[str, Any], expression: dict[str, Any]
) -> None:
    """A filter on items reaches no order (an order owns its items), so counting orders under
    it is held; a filter on orders reaches their items, so the child filter answers as the
    same where filter does for an unfiltered caller."""
    query = _conditional(expression, where=[BEVERAGE])
    if policy is HOT_ITEMS:
        with pytest.raises(SemanticLayerError) as caught:
            _under_row_policy(package, policy, attributes, query)
        assert caught.value.code == "POLICY_DENIED"
        assert caught.value.details["reason"] == "row_filter_uncovered_root"
        return
    prepared = _under_row_policy(package, policy, attributes, query)["prepared_query"]
    own = {**query, "where": [BEVERAGE, _is("dimension.hop_order_customer_id", 10)]}
    config = load_package_config(str(package))
    unfiltered = compile_query(config, Registry(config), {"version": 1, **own})["sql"]
    assert _reference(package, prepared.sql, [10] * len(prepared.parameters)) == _reference(
        package, unfiltered
    )


def test_a_parent_row_policy_still_answers_without_a_child_filter(package: Path) -> None:
    prepared = _under_row_policy(package, OWN_ORDERS, {"customer": 10}, _ask())
    actual = _reference(package, prepared["prepared_query"].sql, [10])
    reference = "SELECT SUM(total) FROM orders WHERE customer_id = 10"
    assert actual == _reference(package, reference) == [(30.0,)]


@pytest.mark.parametrize(
    ("measure", "clause", "child", "expected"),
    [
        ("order_count", BEVERAGE, HAS_BEVERAGE, [(1, 2), (None, 2)]),
        ("order_count", WEB, HAS_WEB_SESSION, [(1, 2), (2, 1), (None, 1)]),
        ("revenue", BEVERAGE, HAS_BEVERAGE, [(1, 30.0), (None, 40.0)]),
    ],
    ids=["count_by_child", "count_by_lookup_then_child", "sum_by_child"],
)
def test_a_lookup_beside_a_child_filter_keeps_parents_it_finds_no_match_for(
    package: Path, measure: str, clause: dict[str, Any], child: str, expected: list[tuple[Any, ...]]
) -> None:
    """Grouped by coupon, orders without one stay in a NULL group, as in the ordinary leaf."""
    rows = _rows(package, _ask(measure, group_by=[COUPON], where=[clause]))
    value = "COUNT(*)" if measure == "order_count" else "SUM(o.total)"
    reference = f"""
        SELECT c.coupon_id, {value} FROM orders o LEFT JOIN coupons c ON c.code = o.coupon_code
        WHERE {child} GROUP BY 1
    """
    assert rows == _reference(package, reference) == _normal(expected)
    ungrouped = _rows(package, _ask(measure, where=[clause]))
    assert ungrouped == _normal([(sum(row[-1] for row in rows),)])


def test_a_lookup_beside_a_child_grouping_keeps_parents_it_finds_no_match_for(
    package: Path,
) -> None:
    """Grouped by type and coupon, the de-duplicated leaf keeps orders 4 and 6 (a beverage
    each, no coupon) under a NULL coupon, so its groups add up to the count by type alone."""
    by_coupon = _rows(package, _ask("order_count", group_by=[TYPE, COUPON]))
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
    assert _rows(package, _ask("order_count", group_by=[TYPE])) == sorted(by_type.items())


def test_a_lookup_joined_outside_exists_is_scanned_again_inside_it(package: Path) -> None:
    """Customers are joined for the group and read again on the path to sessions."""
    query = {
        "select": [_measure("order_count"), _measure("revenue")],
        "group_by": [SEGMENT],
        "where": [WEB],
    }
    rows, sql = _answer(package, query)
    reference = f"""
        SELECT c.segment, COUNT(*), SUM(o.total) FROM orders o
        LEFT JOIN customers c ON c.customer_id = o.customer_id
        WHERE {HAS_WEB_SESSION} GROUP BY 1
    """
    assert rows == _reference(package, reference) == [("retail", 2, 30.0), ("wholesale", 2, 30.0)]
    # Each measure's leaf joins customers outside EXISTS and scans them inside it.
    assert sql.count("LEFT JOIN customers ON") == sql.count("FROM customers\n") == 2


# Orders with a beverage: 1 (customer 10, total 10, two beverages), 2 (10, 20), 4 (12, 40) and
# 6 (11, NULL total). A join to the items would count order 1 twice: a count of 4, a sum of
# 40, a retail sum of 80 and a difference of 50.
@pytest.mark.parametrize(
    ("expression", "value", "expected"),
    [
        (OWN_ORDERS_IF, OWN_ORDERS_SQL, 2),
        (_if("count", _compare("total", ">=", 10)), "COUNT(CASE WHEN o.total >= 10 THEN 1 END)", 3),
        (OWN_REVENUE_IF, OWN_REVENUE_SQL, 30),
        (
            _if("sum", _compare("segment", "=", "retail", "entity.hop_customer"), "total"),
            "SUM(CASE WHEN c.segment = 'retail' THEN o.total END)",
            70,
        ),
        (OWN_SHARE, f"1.0 * {OWN_ORDERS_SQL} / COUNT(*)", 0.5),
        (
            {
                "kind": "arithmetic",
                "op": "subtract",
                "left": "measure.hop.revenue",
                "right": OWN_REVENUE_IF,
            },
            f"SUM(o.total) - {OWN_REVENUE_SQL}",
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
    rows, sql = _answer(package, _conditional(expression, where=[BEVERAGE]))
    reference = f"""
        SELECT {value} FROM orders o LEFT JOIN customers c ON c.customer_id = o.customer_id
        WHERE {HAS_BEVERAGE}
    """
    assert rows == _reference(package, reference) == [(expected,)]
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
            {"expression": _ratio(others_revenue, "measure.hop.revenue"), "as": "share"},
        ],
        "group_by": [COUPON],
        "where": [BEVERAGE],
    }
    others_sql = "CASE WHEN o.customer_id <> 10 THEN o.total END"
    reference = f"""
        SELECT c.coupon_id, COALESCE(SUM({others_sql}), 0), AVG({others_sql}),
          COALESCE(SUM({others_sql}), 0) / NULLIF(SUM(o.total), 0)
        FROM orders o LEFT JOIN coupons c ON c.code = o.coupon_code
        WHERE {HAS_BEVERAGE} GROUP BY 1
    """
    assert (
        _rows(package, query)
        == _reference(package, reference)
        == _normal([(1, 0.0, None, 0.0), (None, 40.0, 40.0, 1.0)])
    )


def test_a_conditional_distinct_count_grouped_by_a_child_dimension(package: Path) -> None:
    """Grouped across the hop, a conditional distinct count counts each order once in every
    product type it holds, as order_count does; order 2 holds both."""
    reference = f"""
        SELECT i.product_type, {OWN_ORDERS_SQL}
        FROM order_items i JOIN orders o ON o.order_id = i.order_id GROUP BY 1
    """
    assert (
        _rows(package, _conditional(OWN_ORDERS_IF, group_by=[TYPE]))
        == _reference(package, reference)
        == [("beverage", 2), ("jaffle", 1)]
    )


@pytest.mark.parametrize(
    "query",
    [FILTERED_SUM, GROUPED_COUNT, _conditional(OWN_REVENUE_IF, where=[BEVERAGE])],
    ids=["filtered", "grouped", "conditional"],
)
def test_the_rewrite_is_disclosed(package: Path, query: dict[str, Any]) -> None:
    result = _run(package, query)
    [warning] = _disclosed(result)
    assert warning["code"] == "REWRITE_APPLIED"
    assert warning["details"]["paths"] == {ITEM: ["relationship.order_items_order"]}
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
    reference = ORDERS_SUM.replace(
        "SUM(o.total)", "AVG(o.total), MAX(o.total), MEDIAN(o.total), COUNT(*)"
    )
    reference = reference.format(HAS_BEVERAGE)
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
    orders = _ask("order_count", group_by=[CATEGORY])
    reference = """
        SELECT p.category, COUNT(DISTINCT o.order_id) FROM orders o
        JOIN order_items i ON i.order_id = o.order_id JOIN products p ON p.sku = i.sku GROUP BY 1
    """
    assert _rows(package, orders) == _reference(package, reference)

    hot = _ask(where=[_is(CATEGORY, "hot")])
    reference = """
        SELECT SUM(o.total) FROM orders o WHERE EXISTS (
          SELECT 1 FROM order_items i JOIN products p ON p.sku = i.sku
          WHERE i.order_id = o.order_id AND p.category = 'hot')
    """
    assert _rows(package, hot) == _reference(package, reference)

    credit = _ask("credit", where=[_is(TYPE, "jaffle")])
    reference = """
        SELECT SUM(c.credit) FROM customers c WHERE EXISTS (
          SELECT 1 FROM orders o JOIN order_items i ON i.order_id = o.order_id
          WHERE o.customer_id = c.customer_id AND i.product_type = 'jaffle')
    """
    # Customer 10 once, though two of its orders hold a jaffle and one holds two beverages.
    assert _rows(package, credit) == _reference(package, reference) == [(300.0,)]

    buyers = _ask("customer_count", group_by=[TYPE])
    reference = """
        SELECT i.product_type, COUNT(DISTINCT o.customer_id)
        FROM order_items i JOIN orders o ON o.order_id = i.order_id GROUP BY 1
    """
    assert _rows(package, buyers) == _reference(package, reference)


# --- Grouped across the hop, an order counts once, whole, in every group ---------------------
# Beverage holds orders 1, 2, 4 and 6 (totals 10, 20, 40 and NULL), jaffle orders 2 and 3 (20 and
# 30): order 2 is in both, so the groups add up to 120 while the orders with an item total 100.
# Order 5 has no item and is in no group; the two orders with a NULL key join none. Here items
# carry revenue of their own, so a sum of order totals by an item dimension could mean either and
# stays refused (KEPT below); a count of orders has one reading. ``bare`` is the same package
# with no item measure, where the orders' whole totals are the only reading. Each reference joins
# every row to its distinct (row, group) pairs, written without the engine.
@pytest.fixture(scope="module")
def bare(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    models = yaml.safe_load(MODELS)
    del models["order_items"]["measures"]
    yield from _serve(_write(tmp_path_factory.mktemp("bare") / "hop", models, ENTITIES))


def _whole(value: str, pairs: str, group: str = "t.product_type", key: str = "order_id") -> str:
    entity = "customers c" if key == "customer_id" else "orders o"
    return (
        f"SELECT {group}, {value} FROM {entity} JOIN ({pairs}) t ON t.{key} = {entity[-1]}.{key} "
        "GROUP BY ALL"
    )


TYPES = "SELECT DISTINCT order_id, product_type FROM order_items"
CATEGORIES = (
    "SELECT DISTINCT i.order_id, p.category FROM order_items i JOIN products p ON p.sku = i.sku"
)
CUSTOMER_TYPES = (
    "SELECT DISTINCT o.customer_id, i.product_type FROM orders o "
    "JOIN order_items i ON i.order_id = o.order_id"
)
REVENUE = {"kind": "measure", "measure": "measure.hop.revenue"}
ORDERS = {"kind": "measure", "measure": "measure.hop.order_count"}
ITEM_REVENUE = {"kind": "measure", "measure": "measure.hop.item_revenue"}
LARGE_ORDERS_IF = _if("count", _compare("total", ">", 15))
WHOLE = {
    # Counts, beside item revenue.
    "row_count": ("package", _ask("order_rows", group_by=[TYPE]), _whole("COUNT(*)", TYPES)),
    "conditional_count": (
        "package",
        _conditional(LARGE_ORDERS_IF, group_by=[TYPE]),
        _whole("COUNT(CASE WHEN o.total > 15 THEN 1 END)", TYPES),
    ),
    # customers -> orders -> items: customer 10 counts once under beverage, though it bought
    # beverages in two orders.
    "two_hops_down": (
        "package",
        _ask("customer_rows", group_by=[TYPE]),
        _whole("COUNT(*)", CUSTOMER_TYPES, key="customer_id"),
    ),
    # Values, where nothing across the hop holds one: an order's payment methods, a customer's
    # session channels (customer 10 has web and app sessions, two of them web).
    "sum_by_payment_method": (
        "package",
        _ask(group_by=[METHOD]),
        _whole("SUM(o.total)", "SELECT DISTINCT order_id, method FROM order_payments", "t.method"),
    ),
    "sum_by_session_channel": (
        "package",
        _ask("credit", group_by=[CHANNEL]),
        _whole(
            "SUM(c.credit)",
            "SELECT DISTINCT customer_id, channel FROM sessions",
            "t.channel",
            "customer_id",
        ),
    ),
    "sum": ("bare", _ask(group_by=[TYPE]), _whole("SUM(o.total)", TYPES)),
    "avg": ("bare", _ask("revenue", "avg", group_by=[TYPE]), _whole("AVG(o.total)", TYPES)),
    "conditional_sum": (
        "bare",
        _conditional(OWN_REVENUE_IF, group_by=[TYPE]),
        _whole("SUM(CASE WHEN o.customer_id = 10 THEN o.total END)", TYPES),
    ),
    # Average order revenue: both parts are the order's own, read from the same orders.
    "ratio_of_its_own_measures": (
        "bare",
        _conditional(_ratio(REVENUE, ORDERS), group_by=[TYPE]),
        _whole("SUM(o.total) / COUNT(DISTINCT o.order_id)", TYPES),
    ),
    # orders -> items -> products: a lookup after the hop.
    "lookup_after_the_hop": (
        "bare",
        _ask(group_by=[CATEGORY]),
        _whole("SUM(o.total)", CATEGORIES, group="t.category"),
    ),
    "beside_a_lookup": (
        "bare",
        _ask(group_by=[TYPE, COUPON]),
        _whole("SUM(o.total)", TYPES, group="t.product_type, c.coupon_id").replace(
            " GROUP BY", " LEFT JOIN coupons c ON c.code = o.coupon_code GROUP BY"
        ),
    ),
    # Q4 2016 holds orders 1, 2, 3, 5 and 6: beverage 30 (1, 2 and 6), jaffle 50 (2 and 3).
    "in_a_window": (
        "bare",
        {**_ask(group_by=[TYPE]), "time": Q4_2016},
        _whole("SUM(o.total)", TYPES).replace(" GROUP BY", f" WHERE {IN_Q4} GROUP BY"),
    ),
    "having": (
        "bare",
        {**_ask(group_by=[TYPE]), "having": [{"field": "revenue", "op": "gt", "value": 60}]},
        _whole("SUM(o.total)", TYPES) + " HAVING SUM(o.total) > 60",
    ),
}


@pytest.mark.parametrize(("fixture", "query", "reference"), WHOLE.values(), ids=WHOLE)
def test_a_grouping_across_the_hop_counts_each_row_whole_in_every_group(
    request: pytest.FixtureRequest, fixture: str, query: dict[str, Any], reference: str
) -> None:
    root = request.getfixturevalue(fixture)
    assert _rows(root, query) == _reference(root, reference)


def test_the_groups_overlap_and_add_up_to_more_than_the_orders(bare: Path) -> None:
    assert _rows(bare, _ask(group_by=[TYPE])) == [("beverage", 70.0), ("jaffle", 50.0)]
    assert _rows(bare, _ask(where=[_is(TYPE, ["beverage", "jaffle"], "is")])) == [(100.0,)]


def test_the_disclosure_names_the_hop_and_says_the_groups_overlap(
    package: Path, bare: Path
) -> None:
    hop = "relationship.order_items_order"
    sentence = (
        f" Grouped by '{TYPE}' across the one-to-many hop '{hop}', each '{ORDER}' counts once, "
        "with its whole value, in every group it has a row in, so the groups can overlap and "
        "need not add up to the total."
    )
    for root, query in [(bare, _ask(group_by=[TYPE])), (package, GROUPED_COUNT)]:
        [warning] = _disclosed(_run(root, query))
        assert warning["details"]["overlapping_groups"] == {
            "group_by": [TYPE],
            "relationships": [hop],
        }
        assert warning["message"].endswith(sentence)
    # A filter splits nothing.
    [filtered] = _disclosed(_run(package, FILTERED_SUM))
    assert "overlapping_groups" not in filtered["details"]
    assert "Grouped by" not in filtered["message"]


ALLOCATION = (
    "is ambiguous across a one-to-many hop: the amount split over the child rows and the full "
    "amount of every row that has one differ. Use a measure at the child's grain, group by a "
    "dimension of the measure's own grain, or filter on the child dimension (each row then "
    "counts once)."
)


def _grouped(aggregation: str, subject: str = "measure 'measure.hop.revenue'") -> str:
    return f"'{aggregation}' of {subject} grouped by a dimension of '{ITEM}' {ALLOCATION}"


# Shapes without one whole-value reading keep their refusal, word for word.
KEPT = {
    # Items carry revenue: order revenue by product type reads as theirs as often.
    "beside_an_item_amount": (_ask(group_by=[TYPE]), _grouped("sum")),
    "average_beside_an_item_amount": (_ask("revenue", "avg", group_by=[TYPE]), _grouped("avg")),
    "conditional_beside_an_item_amount": (
        _conditional(OWN_REVENUE_IF, group_by=[TYPE]),
        _grouped("sum", "an aggregate_if over 'entity.hop_order'"),
    ),
    # Customers -> orders -> items: orders carry revenue on the way.
    "two_hops_beside_order_revenue": (
        _ask("credit", group_by=[TYPE]),
        _grouped("sum", "measure 'measure.hop.credit'"),
    ),
    "median": (_ask("revenue", "median", group_by=[TYPE]), _grouped("median")),
    "non_additive": (
        _ask("score", "avg", group_by=[TYPE]),
        _grouped("avg", "measure 'measure.hop.score'"),
    ),
    # Each order's whole total over the item revenue split across its items.
    "mixed_grain_ratio": (
        _conditional(_ratio(REVENUE, ITEM_REVENUE), group_by=[TYPE]),
        _grouped("sum"),
    ),
    "cumulative": (
        _conditional(
            {"kind": "cumulative", "input": "measure.hop.revenue"}, group_by=[TYPE], time=MONTHLY
        ),
        _grouped("sum"),
    ),
    "group_and_filter_one_child": ({**_ask(group_by=[TYPE]), "where": [HOT_ITEM]}, _grouped("sum")),
}


@pytest.mark.parametrize(("query", "why"), KEPT.values(), ids=KEPT)
def test_shapes_without_one_whole_value_reading_keep_their_refusal(
    package: Path, query: dict[str, Any], why: str
) -> None:
    error = _refusal(package, query)
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert error["why_invalid"] == why


# Where items carry no value, the orders' whole totals are the one reading of a sum, and these
# still refuse as they did: each refusal is the narrower check's, word for word.
KEPT_BARE = {name: KEPT[name] for name in ("median", "cumulative", "group_and_filter_one_child")}
KEPT_BARE["max"] = (_ask("revenue", "max", group_by=[TYPE]), _grouped("max"))


@pytest.mark.parametrize(("query", "why"), KEPT_BARE.values(), ids=KEPT_BARE)
def test_without_an_item_amount_the_other_shapes_keep_their_refusal(
    bare: Path, query: dict[str, Any], why: str
) -> None:
    error = _refusal(bare, query)
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert error["why_invalid"] == why


def test_a_ratio_with_a_measure_of_another_entity_keeps_its_refusal(bare: Path) -> None:
    """With no item amount, order revenue by type answers, but order revenue per item row
    still sets an order's whole total against counts split over its items."""
    item_rows = _if("count", _compare("is_hot", "=", True, ITEM))
    error = _refusal(bare, _conditional(_ratio(REVENUE, item_rows), group_by=[TYPE]))
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert error["why_invalid"] == _grouped("sum")


def test_totals_over_the_grouping_count_each_order_once(bare: Path) -> None:
    """Each total is the call without the grouping: every order's total once, the two with a
    NULL key and order 5 with no item included (290), never the 120 the groups add up to."""
    assert _reference(bare, "SELECT SUM(total) FROM orders") == [(290.0,)]
    rows = typed_rows(_run(bare, {**_ask(group_by=[TYPE]), "totals": []}))
    assert _normal((row[TYPE], row["revenue"]) for row in rows) == _normal(
        [("beverage", 70.0), ("jaffle", 50.0), (None, 290.0)]
    )
    total = {"kind": "total", "input": "measure.hop.revenue", "over": [TYPE]}
    beside = {"select": [_measure("revenue"), {"expression": total, "as": "all_orders"}]}
    beside["group_by"] = [TYPE]
    assert _rows(bare, beside) == [("beverage", 70.0, 290.0), ("jaffle", 50.0, 290.0)]


def _share(measure: str, over: list[str]) -> dict[str, Any]:
    return {
        "expression": {"kind": "share", "input": f"measure.hop.{measure}", "over": over},
        "as": "share",
    }


@pytest.mark.parametrize(("fixture", "measure"), [("bare", "revenue"), ("package", "order_count")])
def test_a_share_over_the_overlapping_groups_is_refused(
    request: pytest.FixtureRequest, fixture: str, measure: str
) -> None:
    """Its total would be the sum of the groups: 6 orders for the 5 with an item, so a share of
    the distinct count read 4/6 for beverage."""
    query = {"select": [_measure(measure), _share(measure, [TYPE])], "group_by": [TYPE]}
    error = _refusal(request.getfixturevalue(fixture), query)
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert error["why_invalid"] == (
        f"A share over '{TYPE}' is not supported: each '{ORDER}' counts in every group it has "
        f"a '{ITEM}' row in, so the groups overlap and the share's total, their sum, counts it "
        "once per group. Select a total over the same columns beside the groups (kind: total), "
        "which counts each once, and divide by its column after the call."
    )


def test_a_share_within_each_group_stays_answered(bare: Path) -> None:
    """Inside one product type each order appears once, so its coupons' shares add up to 1."""
    query = {
        "select": [_measure("revenue"), _share("revenue", [COUPON])],
        "group_by": [TYPE, COUPON],
    }
    reference = """
        SELECT t.product_type, c.coupon_id, SUM(o.total),
          SUM(o.total) / SUM(SUM(o.total)) OVER (PARTITION BY t.product_type)
        FROM orders o JOIN (SELECT DISTINCT order_id, product_type FROM order_items) t
          ON t.order_id = o.order_id
        LEFT JOIN coupons c ON c.code = o.coupon_code GROUP BY 1, 2
    """
    assert _rows(bare, query) == _reference(bare, reference)


@pytest.mark.parametrize("query", [GROUPED_COUNT, _ask(group_by=[TYPE])], ids=["count", "sum"])
def test_a_child_row_policy_holds_the_grouping_as_it_holds_a_count(
    bare: Path, query: dict[str, Any]
) -> None:
    """A filter on items reaches no order, so counting or summing orders under it is held."""
    with pytest.raises(SemanticLayerError) as caught:
        _under_row_policy(bare, HOT_ITEMS, {"hot": True}, query)
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details["reason"] == "row_filter_uncovered_root"


def test_a_parent_row_policy_filters_the_grouped_orders(bare: Path) -> None:
    """A filter on orders reaches their items: customer 10's orders 1 and 2 alone."""
    query = {"observation_scope": "query", **_ask(group_by=[TYPE])}
    prepared = _under_row_policy(bare, OWN_ORDERS, {"customer": 10}, query)["prepared_query"]
    actual = _reference(bare, prepared.sql, [10] * len(prepared.parameters))
    reference = _whole("SUM(o.total)", TYPES).replace(
        " GROUP BY", " WHERE o.customer_id = 10 GROUP BY"
    )
    assert actual == _reference(bare, reference) == [("beverage", 30.0), ("jaffle", 20.0)]


def test_a_hidden_item_reads_as_a_package_without_items(bare: Path) -> None:
    """The route to a product passes an item: hidden, it is no route, as if items didn't exist."""
    base = load_package_config(str(bare))
    config = with_policies(base, visibility_policy("hidden", ITEM))
    query = {"observation_scope": "query", **_ask(group_by=[CATEGORY])}
    # What hiding the entity hides with it, for the package without items.
    gone = [
        ITEM,
        *(row.id for row in base.relationships if ITEM in (row.source_entity, row.target_entity)),
        *(row.id for row in [*base.dimensions, *base.measures] if row.entity == ITEM),
    ]
    reader = RequestContext(roles=CALLER["roles"])

    def validate(package_config: Any, **kwargs: Any) -> dict[str, Any]:
        runtime = opened(Runtime.from_config(package_config, source_path=str(bare)))
        try:
            return runtime.validate(query, **kwargs)
        finally:
            runtime.close()

    assert validate(config)["ok"] is True  # the author's view answers
    hidden = validate(config, context=reader)
    assert hidden["ok"] is False
    assert hidden["errors"][0]["code"] == "PATH_NOT_FOUND"
    assert envelope(hidden) == envelope(validate(absent(config, gone), context=reader))


@pytest.mark.parametrize("warehouse", ["postgres", "clickhouse"])
def test_other_dialects_render_the_same_whole_value_leaf(bare: Path, warehouse: str) -> None:
    """Postgres's and ClickHouse's SQL keep one row per (order, type) before they sum, and
    return DuckDB's rows when DuckDB runs them (the live Postgres check is the shop ledger)."""
    query = {"observation_scope": "query", **_ask(group_by=[TYPE])}
    sql = _compile(_config(bare, warehouse), query)["sql"].removesuffix(
        "\nSETTINGS join_use_nulls = 1"
    )
    assert "SELECT DISTINCT\n  orders.order_id AS __entity_key_1," in sql
    assert "SUM(leaf_1__leaf_1_entity_rows.__entity_value)" in sql
    assert _reference(bare, sql) == _rows(bare, query)


RUNNING_REVENUE = {"kind": "cumulative", "input": "measure.hop.revenue"}
NEGATED = "'has a row that is not X' and 'has no row that is X' differ"
TWO_CONDITIONS = "both cross a one-to-many hop"


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        # A median or a maximum by product type is no whole-value count (see WHOLE below).
        (_ask("revenue", "median", group_by=[TYPE]), "is ambiguous across"),
        (_ask("revenue", "max", group_by=[TYPE]), "is ambiguous across"),
        # A measure's own filter has no child scope to state (query filters do: see below).
        (_bound_filter(_is(TYPE, "beverage", "!=")), NEGATED),
        # At most one condition may cross a one-to-many hop: with two, one row may have to meet
        # both, or any rows each. On siblings under a shared hop, or both groups, and a
        # measure's own filter cannot bypass the rule.
        ({**GROUPED_COUNT, "where": [HOT_ITEM]}, TWO_CONDITIONS),
        ({**GROUPED_COUNT, "where": [_is(CATEGORY, "hot")]}, TWO_CONDITIONS),
        (_ask("credit", where=[_is(TYPE, "jaffle"), _is(METHOD, "card")]), TWO_CONDITIONS),
        (_ask("customer_count", group_by=[TYPE], where=[_is(METHOD, "card")]), TWO_CONDITIONS),
        (_ask("customer_count", group_by=[TYPE, METHOD]), TWO_CONDITIONS),
        ({**_bound_filter(WEB), "where": [BEVERAGE]}, TWO_CONDITIONS),
        # On a boolean only "= true" reads one way.
        (_bound_filter(_is(HOT, True, "<")), NEGATED),
        (_bound_filter(_is(HOT, 0)), NEGATED),
        # orders -> customer -> sessions: many-to-many through the customer.
        (_ask("order_count", group_by=[CHANNEL]), "many-to-many"),
        # Grouping still requires the declared parent key, unlike filter-only EXISTS.
        (_ask("face_value", group_by=[TYPE]), "join off the declared key"),
        # A pre-aggregated value has no one row per key to count.
        (_ask("score", "avg", where=[BEVERAGE]), "not defined over one row"),
        (
            _conditional(RUNNING_REVENUE, where=[BEVERAGE], time=MONTHLY),
            "not supported across a one-to-many hop",
        ),
        # A conditional aggregate, alone or inside a ratio, refuses what its measure refuses.
        (
            _conditional(_if("max", _compare("customer_id", "=", 10), "total"), group_by=[TYPE]),
            "is ambiguous across",
        ),
        (_conditional(OWN_REVENUE_IF, where=[BEVERAGE, WEB]), TWO_CONDITIONS),
        (_conditional(OWN_SHARE, group_by=[TYPE], where=[HOT_ITEM]), TWO_CONDITIONS),
        (_conditional(OWN_ORDERS_IF, group_by=[CHANNEL]), "many-to-many"),
    ],
    ids=[
        "median",
        "max",
        "measure_filter",
        "group_and_filter_one_child",
        "group_and_filter_through_a_lookup",
        "sibling_filters",
        "group_and_sibling_filter",
        "sibling_groups",
        "measure_filter_beside_a_sibling_filter",
        "measure_filter_boolean_less_than_true",
        "measure_filter_boolean_zero",
        "many_to_many",
        "off_key_join",
        "non_additive",
        "cumulative",
        "conditional_grouped_max",
        "conditional_two_filters",
        "conditional_ratio_group_and_filter",
        "conditional_many_to_many",
    ],
)
def test_ambiguous_shapes_stay_refused(package: Path, query: dict[str, Any], reason: str) -> None:
    error = _refusal(package, query)
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert reason in error["why_invalid"]
    assert error["recovery_hints"]


PAID_SHARE = _ratio(PAID_IF, {"kind": "measure", "measure": "measure.hop.order_count"})
PAID_REFERENCE = "SELECT SUM(amount) FROM payments p WHERE " + _has_item(
    "i.product_type = 'beverage'", "p.order_id"
)
PAID_QUESTIONS = {
    "measure": (_ask("paid", where=[BEVERAGE]), PAID_REFERENCE),
    "conditional": (_conditional(PAID_IF, where=[BEVERAGE]), PAID_REFERENCE),
    "conditional_ratio": (
        _conditional(PAID_SHARE, where=[BEVERAGE]),
        f"SELECT ({PAID_REFERENCE}) / ({ORDERS_COUNT.format(HAS_BEVERAGE)})",
    ),
}


@pytest.mark.parametrize(("query", "reference"), PAID_QUESTIONS.values(), ids=PAID_QUESTIONS)
def test_payments_of_orders_with_a_child_value(
    package: Path, query: dict[str, Any], reference: str
) -> None:
    """Each payment is a receipt, a child of its order, so each payment of an order that
    included a beverage counts once: order 1's two payments of 5 stay two."""
    assert _rows(package, query) == _reference(package, reference)


@pytest.mark.parametrize(("query", "reference"), PAID_QUESTIONS.values(), ids=PAID_QUESTIONS)
def test_rows_finer_than_their_entity_stay_refused(
    package: Path, query: dict[str, Any], reference: str
) -> None:
    """A config built in memory can still hold a measure whose rows are finer than its entity's
    key; one row per key would merge two payments of one receipt with equal amounts."""
    config = _edited(package, "measures", "measure.hop.paid", row_grain=["payment_id"])
    details = _compile_refusal(config, query, "MIXED_GRAIN_INVALID")
    assert "rows are finer than its entity" in details["why_invalid"]


def _readings(any_not: str, none: str, value: str = "SUM(o.total)") -> dict[str, str]:
    """The two readings of a negated child filter, as reference SQL."""
    return {
        "any_not": f"SELECT {value} FROM orders o WHERE {_has_item(any_not)}",
        "none": f"SELECT {value} FROM orders o WHERE NOT {_has_item(none)}",
    }


@pytest.mark.parametrize(
    ("query", "readings"),
    [
        # "Orders without a beverage" and "orders with a non-beverage item" differ.
        (
            _where("!=", "beverage"),
            _readings("i.product_type != 'beverage'", "i.product_type = 'beverage'"),
        ),
        (
            _where("<>", "beverage"),
            _readings("i.product_type <> 'beverage'", "i.product_type = 'beverage'"),
        ),
        (
            _where("NOT IN", ["beverage"]),
            _readings("i.product_type NOT IN ('beverage')", "i.product_type IN ('beverage')"),
        ),
        (
            _where("missing", True),
            _readings("i.product_type IS NULL", "i.product_type IS NOT NULL"),
        ),
        # On a boolean only "= true" reads one way.
        (_where("=", False, HOT), _readings("i.is_hot = false", "i.is_hot != false")),
        (_where("=", "false", HOT), _readings("i.is_hot = false", "i.is_hot != false")),
        (_where("in", [False], HOT), _readings("i.is_hot IN (false)", "i.is_hot NOT IN (false)")),
        # One item that is both, or a beverage and some jaffle?
        (
            _ask(where=[BEVERAGE, {**BEVERAGE, "value": "jaffle"}]),
            {
                "same_row": ORDERS_SUM.format(
                    _has_item("i.product_type = 'beverage' AND i.product_type = 'jaffle'")
                ),
                "separate_rows": ORDERS_SUM.format(f"{HAS_BEVERAGE} AND {HAS_JAFFLE}"),
            },
        ),
        # Every leaf of an expression reads the child scope the same way, operand by operand.
        (
            _conditional(OWN_REVENUE_IF, where=[{**BEVERAGE, "op": "!="}]),
            _readings(
                "i.product_type != 'beverage'", "i.product_type = 'beverage'", OWN_REVENUE_SQL
            ),
        ),
        (
            _conditional(OWN_SHARE, where=[{**BEVERAGE, "op": "NOT IN", "value": ["x"]}]),
            _readings(
                "i.product_type NOT IN ('x')",
                "i.product_type IN ('x')",
                f"{OWN_ORDERS_SQL} * 1.0 / COUNT(DISTINCT o.order_id)",
            ),
        ),
    ],
    ids=[
        "not_equal",
        "angle_not_equal",
        "not_in",
        "missing",
        "boolean_false",
        "boolean_false_text",
        "boolean_in_false",
        "two_filters_one_child",
        "conditional_not_equal",
        "conditional_ratio_not_in",
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
        resent = {**query, "where": option["where"]}
        assert _rows(package, resent) == _reference(package, readings[option["id"]]), option["id"]


@pytest.mark.parametrize(
    ("query", "details"),
    [
        # The engine's own null-test spellings, and one with a hint but no alias.
        *[
            (_where(op, "beverage"), {"path": "where[0].op"})
            for op in ("IS NULL", "IS DISTINCT FROM", "NOT LIKE")
        ],
        # A boolean compares with is or not: once unknowns count, a range has no exact
        # complement, so there is no reading to offer.
        (_where("<", True, HOT), {"dimension": HOT}),
    ],
    ids=["is_null", "is_distinct_from", "not_like", "boolean_range"],
)
def test_filters_without_a_filter_word_are_refused_before_hop_analysis(
    package: Path, query: dict[str, Any], details: dict[str, str]
) -> None:
    error = _refusal(package, query)
    assert error["code"] == "INVALID_QUERY"
    assert details.items() <= error["details"].items()
    assert [hint["code"] for hint in error["recovery_hints"]] == ["USE_FILTER_WORD"]


def test_positive_null_and_boolean_tests_mean_exists(package: Path) -> None:
    for query, predicate in [
        (_where("missing", False), "i.product_type IS NOT NULL"),
        (_where("=", True, HOT), "i.is_hot"),
    ]:
        assert _rows(package, query) == _reference(package, ORDERS_SUM.format(_has_item(predicate)))


def test_a_refusal_names_the_path_at_fault(package: Path) -> None:
    """The grouped sum is at fault, not the filter beside it, so the allocation hint shows."""
    error = _refusal(package, _ask(group_by=[TYPE], where=[_is(CATEGORY, "hot", "is")]))
    assert error["details"]["purpose"] == "group_by"
    assert error["details"]["target_entity"] == ITEM
    kinds = [hint["kind"] for hint in error["recovery_hints"]]
    assert "requires_allocation_policy" in kinds


def test_a_refusal_never_offers_a_different_measure(package: Path) -> None:
    """A recovery used to hand back item revenue in place of order revenue, ready to run."""
    error = _refusal(package, _ask("revenue", "median", group_by=[TYPE]))
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
            "select": [
                {"expression": "measure.jaffle.order_count", "as": "orders"},
                {"expression": "measure.jaffle.item_revenue_usd", "as": "revenue"},
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
        select = [{"expression": measure, "as": "value"}]
        return runtime.validate({"select": select, "group_by": group_by})

    try:
        report = ask(groups)
        alone = [ask([group]) for group in groups]
    finally:
        runtime.close()
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "MIXED_GRAIN_INVALID"
    assert TWO_CONDITIONS in error["why_invalid"]
    for single in alone:
        assert [step["kind"] for step in single["logical_plan"]["rewrite_steps"]] == [
            "entity_in_terms_of"
        ]


# The de-duplicated leaf on the locally testable dialect families: correlated EXISTS, or (on
# ClickHouse, and for a group across the hop everywhere) a CTE of SELECT DISTINCT rows and an
# aggregate over its columns. Only the time bucket and the median differ by dialect.
_TRUNC = "DATE_TRUNC('month', CAST(orders.ordered_at AS TIMESTAMP))"
MONTH = {"duckdb": _TRUNC, "postgres": _TRUNC, "clickhouse": f"CAST({_TRUNC} AS TIMESTAMP)"}
_VALUE = "orders.total"
MEDIAN = {
    "duckdb": f"MEDIAN({_VALUE})",
    "postgres": f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {_VALUE} ASC)",
    "clickhouse": f"quantileExactInclusive(0.5)({_VALUE})",
}
SHAPES = {
    "filtered": FILTERED_SUM,
    "grouped": GROUPED_COUNT,
    "monthly": {**FILTERED_SUM, "time": MONTHLY},
    "median": {
        "select": [_measure("revenue", "median", alias="median_revenue")],
        "where": [BEVERAGE],
    },
}
# Parts both leaf forms share: the beverage test of the EXISTS leaf, the ClickHouse leaf's
# distinct rows, and a revenue total's guard (which settles empty groups) and outputs.
SQL_PARTS = {
    "exists": """WHERE
  EXISTS (
SELECT
  1 AS match
FROM order_items
WHERE
  orders.order_id = order_items.order_id
  AND order_items.product_type = 'beverage'
)""",
    "rows": """WITH leaf_1__leaf_1_entity_rows AS (
SELECT DISTINCT
  orders.order_id AS __entity_key_1,
  orders.total AS __entity_value,
  1 AS __entity_rows
FROM orders
INNER JOIN order_items ON orders.order_id = order_items.order_id
WHERE
  order_items.product_type = 'beverage'
),""",
    "total": """guarded_base AS (
SELECT
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.m1 AS "revenue"
FROM guarded_base AS base""",
    "monthly_total": """guarded_base AS (
SELECT
  base.t AS t,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.t AS "period",
  base.m1 AS "revenue"
FROM guarded_base AS base
ORDER BY
  "period" ASC""",
}
DIALECT_SQL = {
    "filtered": """WITH leaf_1 AS (
SELECT
  SUM(orders.total) AS m1,
  COUNT(1) AS m1_rows
FROM orders
{exists}
),
{total}""",
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
  base.m1 AS "orders"
FROM guarded_base AS base""",
    "monthly": """WITH leaf_1 AS (
SELECT
  {month} AS t,
  SUM(orders.total) AS m1,
  COUNT(1) AS m1_rows
FROM orders
{exists}
GROUP BY
  {month}
),
{monthly_total}""",
    # A median overrides the revenue sum, so the answer counts the rows it read.
    "median": """WITH leaf_1 AS (
SELECT
  {median} AS m1,
  COUNT(1) AS "m1_rows"
FROM orders
{exists}
)
SELECT
  base.m1 AS "median_revenue",
  base."m1_rows" AS "m1_rows"
FROM leaf_1 AS base""",
}


CLICKHOUSE_SQL = {
    "filtered": """{rows}
leaf_1 AS (
SELECT
  SUM(leaf_1__leaf_1_entity_rows.__entity_value) AS m1,
  COUNT(leaf_1__leaf_1_entity_rows.__entity_rows) AS m1_rows
FROM leaf_1__leaf_1_entity_rows
),
{total}""",
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
{monthly_total}""",
    "median": """{rows}
leaf_1 AS (
SELECT
  {median} AS m1,
  COUNT(leaf_1__leaf_1_entity_rows.__entity_rows) AS "m1_rows"
FROM leaf_1__leaf_1_entity_rows
)
SELECT
  base.m1 AS "median_revenue",
  base."m1_rows" AS "m1_rows"
FROM leaf_1 AS base""",
}


# Every shape, each dialect's median, and each ClickHouse template; the filtered leaf's
# correlated EXISTS is pinned for the remote dialects below.
@pytest.mark.parametrize(
    ("warehouse", "shape"),
    [
        ("duckdb", "grouped"),
        ("duckdb", "monthly"),
        ("duckdb", "median"),
        ("postgres", "median"),
        ("clickhouse", "filtered"),
        ("clickhouse", "monthly"),
        ("clickhouse", "median"),
    ],
)
def test_each_dialect_renders_the_de_duplicated_leaf(
    package: Path, warehouse: str, shape: str
) -> None:
    query = {"observation_scope": "query", **SHAPES[shape]}  # as in _run
    sql = _compile(_config(package, warehouse), query)["sql"]
    templates = CLICKHOUSE_SQL if warehouse == "clickhouse" and shape != "grouped" else DIALECT_SQL
    value = (
        "leaf_1__leaf_1_entity_rows.__entity_value" if warehouse == "clickhouse" else "orders.total"
    )
    median = MEDIAN[warehouse].replace("orders.total", value)
    expected = templates[shape].format(month=MONTH[warehouse], median=median, **SQL_PARTS)
    # ClickHouse reads an unmatched outer-join field as NULL only with this setting.
    assert sql == expected + ("\nSETTINGS join_use_nulls = 1" if warehouse == "clickhouse" else "")


@pytest.mark.parametrize("warehouse", ["snowflake", "bigquery", "databricks"])
def test_remote_dialects_render_the_filter_as_correlated_exists(
    package: Path, warehouse: str
) -> None:
    query = {"observation_scope": "query", **FILTERED_SUM}  # as in _run
    sql = _compile(_config(package, warehouse), query)["sql"]
    quote = "`" if warehouse in {"bigquery", "databricks"} else '"'
    assert sql == DIALECT_SQL["filtered"].format(**SQL_PARTS).replace('"', quote)
