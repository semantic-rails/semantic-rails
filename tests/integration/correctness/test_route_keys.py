"""An entity's key reads where the rows name it, on DuckDB and Postgres.

Refunds reach their customer through their order (refunds, then orders, then customers), and
orders name their customer themselves. Customer 108 hasn't signed up: the customers table
doesn't hold it, but order 11 names it. These tests give order 11 a goods refund of 1 (the
shop fixture has none), so "net revenue by customer" reads 108 at 2 - 1 = 1, its refund beside
its order under its own key, never under a NULL customer. Grouped by an attribute of the
customer (the signup channel), 108 has none, so its refund is in the NULL group. Hand SQL reads
the raw tables: a refund's customer is its order's ``customer_id``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from semantic_rails.runtime import Runtime

from .conftest import SEED, SESSION_ZONE, _postgres, _rows, _strict, _write_variant
from .conftest import _runtime as _shop_runtime

CUSTOMER = "dimension.shop_customer_id"
CHANNEL = "dimension.shop_customer_channel"
NAME = "dimension.shop_customer_name"
REVENUE, GOODS = "measure.shop.revenue", "measure.shop.goods_refunded"
NET = {"kind": "arithmetic", "op": "subtract", "left": REVENUE, "right": GOODS}
NOW = "2024-10-15T00:00:00Z"
REFUND_OF_108 = (
    "INSERT INTO refunds (refund_id, order_id, refund_type, goods_amount, shipping_amount, "
    "tax_amount) VALUES (8, 11, 'goods', 1.00, NULL, NULL);\n"
)
# Each customer's revenue less goods refunds, the refunds read through their orders. A side
# with no rows for the customer reads 0 (both have data); one whose rows are all NULL reads
# NULL (105's only order has no amount, 103's only refund is shipping).
NET_SQL = """
WITH o AS (SELECT customer_id, SUM(amount) AS v FROM orders GROUP BY customer_id),
r AS (
  SELECT o.customer_id, SUM(r.goods_amount) AS v
  FROM refunds AS r LEFT JOIN orders AS o ON o.order_id = r.order_id
  GROUP BY o.customer_id
)
SELECT COALESCE(o.customer_id, r.customer_id),
  CASE WHEN o.customer_id IS NULL THEN 0 ELSE o.v END
    - CASE WHEN r.customer_id IS NULL THEN 0 ELSE r.v END
FROM o FULL JOIN r ON o.customer_id = r.customer_id
"""


@pytest.fixture(scope="module", params=["duckdb", "postgres"])
def shop(request, tmp_path_factory) -> Iterator[Runtime]:
    """The shop package with a goods refund on order 11, on DuckDB or in a Postgres schema of
    its own (dropped afterwards)."""
    package = _write_variant(tmp_path_factory.mktemp("route_keys"), "utc_authored")
    seed = SEED.rstrip() + "\n" + REFUND_OF_108
    (package / "data" / "seed.sql").write_text(seed, encoding="utf-8")
    if request.param == "duckdb":
        runtime = _shop_runtime(package)
        try:
            _rows(runtime, f"SET GLOBAL TimeZone = '{SESSION_ZONE}'")
            yield runtime
        finally:
            runtime.close()
        return
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    if any(not os.environ.get(name, "").strip() for name in required):
        pytest.skip("postgres: SR_POSTGRES_* unset (run under with_postgres.sh)")
    schema = f"sr_route_keys_{uuid.uuid4().hex[:12]}"
    options = {
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    }
    admin = _postgres(package, options)
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__})")
    runtime = _postgres(package, {**options, "schema": schema})
    try:
        _rows(runtime, f"SET TimeZone = '{SESSION_ZONE}'")
        _rows(runtime, seed)
        yield runtime
    finally:
        runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def _number(value: Any) -> float | None:
    return None if value is None else float(Decimal(str(value)))


def _by(rows: list[dict[str, Any]], key: str, value: str) -> dict[Any, float | None]:
    found = {row[key]: _number(row[value]) for row in rows}
    assert len(found) == len(rows)
    return found


def _hand(runtime: Runtime, sql: str) -> dict[Any, float | None]:
    return {key: _number(value) for key, value in _rows(runtime, sql)}


def test_goods_refunded_by_customer_reads_each_refund_under_its_orders_customer(shop: Runtime):
    """108's refund reads under 108, never a NULL customer."""
    answer = shop.query({"select": [GOODS], "group_by": [CUSTOMER]}, now=NOW)
    hand = _hand(
        shop,
        "SELECT o.customer_id, SUM(r.goods_amount) FROM refunds AS r "
        "LEFT JOIN orders AS o ON o.order_id = r.order_id GROUP BY o.customer_id",
    )
    assert _by(answer["rows"], CUSTOMER, "goods_refunded") == hand
    assert hand == {102: 10.0, 103: None, 105: 2.0, 108: 1.0}


def test_net_revenue_by_customer_settles_a_customer_who_hasnt_signed_up(shop: Runtime):
    """108 reads 2 - 1 = 1: its refund beside its order (it read 2, the refund in a NULL row)."""
    call = {"select": [{"expression": NET, "as": "net"}], "group_by": [CUSTOMER]}
    answer = shop.query(call, now=NOW)
    assert _by(answer["rows"], CUSTOMER, "net") == _hand(shop, NET_SQL)
    assert _by(answer["rows"], CUSTOMER, "net")[108] == 1.0
    # Filtered by the key, the refund reads the same customer.
    (row,) = shop.query(
        {"select": [{"expression": NET, "as": "net"}],
         "where": [{"field": CUSTOMER, "op": "is", "value": 108}]},
        now=NOW,
    )["rows"]  # fmt: skip
    assert _number(row["net"]) == 1.0
    # No refund's order is a guest order: data of nothing.
    (row,) = shop.query(
        {"select": [GOODS], "where": [{"field": CUSTOMER, "op": "missing", "value": True}]},
        now=NOW,
    )["rows"]
    assert _number(row["goods_refunded"]) == 0.0


def test_net_revenue_by_customer_and_month_reads_108_in_august(shop: Runtime):
    """Order 11 and its refund fall in August 2024 (the refund on its order's clock)."""
    call = {
        "select": [{"expression": NET, "as": "net"}],
        "group_by": [CUSTOMER],
        "time": {
            "temporal_role": "temporal_role.shop_order_ordered_at",
            "grain": "month",
            "start": "2024-08-01",
            "end": "2024-09-01",
        },
    }
    rows = shop.query(call, now=NOW)["rows"]
    assert {row[CUSTOMER]: _number(row["net"]) for row in rows} == {108: 1.0}


def test_grouped_by_the_customers_name_108_reads_under_its_key(shop: Runtime):
    """A name groups by the key too: 108 has no name, but its own key, so one row."""
    answer = shop.query({"select": [{"expression": NET, "as": "net"}], "group_by": [NAME]}, now=NOW)
    rows = {(row[CUSTOMER], row[NAME]): _number(row["net"]) for row in answer["rows"]}
    assert rows[(108, None)] == 1.0
    assert (None, None) not in rows


def test_grouped_by_the_signup_channel_108_has_none(shop: Runtime):
    """An attribute of the customer reads the customers table: 108's refund is in the NULL
    channel, beside 105's (no channel), 2 + 1."""
    answer = shop.query({"select": [GOODS], "group_by": [CHANNEL]}, now=NOW)
    hand = _hand(
        shop,
        "SELECT s.channel, SUM(r.goods_amount) FROM refunds AS r "
        "LEFT JOIN orders AS o ON o.order_id = r.order_id "
        "LEFT JOIN signups AS s ON s.customer_id = o.customer_id GROUP BY s.channel",
    )
    assert _by(answer["rows"], CHANNEL, "goods_refunded") == hand
    assert hand[None] == 3.0


def test_average_net_revenue_per_customer_counts_108_at_1(shop: Runtime):
    """Per customer, net revenue is -1 (102), 1 (108), 8 (104), 9 (106) and 23 (101); 103 and
    105 have none. Their average is 8 (it read 8.2, with 108 at 2)."""
    call = {
        "select": [
            {
                "expression": {
                    "kind": "distribution",
                    "function": "avg",
                    "over": {
                        "kind": "entity_value",
                        "entity": "entity.shop_customer",
                        "input": NET,
                    },
                },
                "as": "avg_net",
            }
        ]
    }
    (row,) = shop.query(call, now=NOW)["rows"]
    (hand,) = _rows(shop, f"SELECT AVG(v) FROM ({NET_SQL}) AS per_customer (customer_id, v)")
    assert _number(row["avg_net"]) == pytest.approx(_number(hand[0])) == pytest.approx(8.0)
