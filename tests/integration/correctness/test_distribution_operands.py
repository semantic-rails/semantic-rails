"""A distribution over several measures settles each entity's operands before it aggregates.

"Median net order value" is revenue less goods refunds per order: an order with no refund row
refunded nothing (0) where refunds have data in scope, and the ledger pins its answers
(shop/median-net-order-value-*). Here: the refusal a user sees where the per-entity values
can't be settled, the entities a bounded window reads, and a declared sum whose CASE has no
ELSE beside revenue, on DuckDB and Postgres.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import yaml

from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

from .conftest import SEED, SESSION_ZONE, _postgres, _rows, _strict, _write_variant
from .conftest import _runtime as _shop_runtime

ROLE = "temporal_role.shop_order_ordered_at"
NET = {
    "kind": "arithmetic",
    "op": "subtract",
    "left": "measure.shop.revenue",
    "right": "measure.shop.goods_refunded",
}
NET_MEDIAN = {
    "expression": {
        "kind": "distribution",
        "function": "median",
        "over": {"kind": "entity_value", "entity": "entity.shop_order", "input": NET},
    },
    "as": "median_net",
}
MONTH = {"temporal_role": ROLE, "grain": "month"}
# Repeat customers (two orders or more, ever) who signed up on the web: 101 and 103.
REPEAT = {
    "entity": "entity.shop_customer",
    "having": [{"field": "measure.shop.order_count", "op": "gte", "value": 2}],
    "over": "all_time",
}
WEB = {"field": "dimension.shop_customer_channel", "op": "is", "value": "web"}
NOW = "2024-10-15T00:00:00Z"


def _runtime(request, backend):
    return request.getfixturevalue(f"{backend}_backend").runtimes["utc_authored"]


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_where_beside_an_entity_filter_refuses_naming_the_distribution(request, backend):
    """Under the dataset scope a where beside an entity filter can't be told apart from it, so
    the per-entity values can't be settled: the call refuses as "net by order" does alone,
    naming the distribution's own output, and answers under the query scope."""
    runtime = _runtime(request, backend)
    call = {"select": [NET_MEDIAN], "time": MONTH, "where": [REPEAT, WEB]}
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(call, now=NOW)
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert raised.value.details == {"outputs": ["median_net"], "observation_scope": "dataset"}
    message = str(raised.value)
    assert message.startswith("Can't tell whether median_net has data outside this query's where")
    assert "Resend with observation_scope 'query'" in message
    assert "__entity_value" not in message

    alone = {"select": [{"expression": NET, "as": "net"}], "group_by": ["dimension.shop_order_id"]}
    with pytest.raises(SemanticLayerError) as alone_raised:
        runtime.query({**call, **alone}, now=NOW)
    assert alone_raised.value.code == "EMPTY_GROUPS_UNSETTLED"

    # Inside the filters these customers have no goods refund amount (order 4's refund is
    # shipping only), so goods refunds have no data in scope and each net value is NULL.
    answer = runtime.query({**call, "observation_scope": "query"}, now=NOW)
    assert [row["median_net"] for row in answer["rows"]] == [None] * 5

    # One measure's leaf holds every value its entities have: nothing to settle, so the median
    # order under the same filters answers (orders 1, 3, 4, 8 and 9).
    median_order = {
        "expression": {
            **NET_MEDIAN["expression"],
            "over": {**NET_MEDIAN["expression"]["over"], "input": "measure.shop.revenue"},
        },
        "as": "median_order",
    }
    answer = runtime.query({**call, "select": [median_order]}, now=NOW)
    assert [float(row["median_order"]) for row in answer["rows"]] == [10.0, 7.0, 20.0, 6.0, 3.0]


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_median_aov_of_repeat_web_customers_answers_under_the_dataset_scope(request, backend):
    """The declared AOV reads revenue and order count from the same order rows: under an entity
    filter each gets its own leaf, but both hold the same customers, so nothing needs settling
    and the default scope answers. Customers 101 (23 / 3) and 103 (23 / 2)."""
    runtime = _runtime(request, backend)
    median_aov = {
        "expression": {
            "kind": "distribution",
            "function": "median",
            "over": {
                "kind": "entity_value",
                "entity": "entity.shop_customer",
                "input": "metric.shop.average_order_value",
            },
        },
        "as": "median_aov",
    }
    call = {"select": [median_aov], "where": [REPEAT, WEB]}
    (row,) = runtime.query(call, now=NOW)["rows"]
    assert float(row["median_aov"]) == pytest.approx(23 / 3 / 2 + 23 / 2 / 2)
    quarters = runtime.query({**call, "time": {"temporal_role": ROLE, "grain": "quarter"}}, now=NOW)
    assert [float(row["median_aov"]) for row in quarters["rows"]] == [8.5, 20.0, 4.5]


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_an_entity_filter_alone_settles_each_order(request, backend):
    """Repeat customers' orders (1, 2, 3, 4, 6, 8, 9): orders with no refund read their amount."""
    runtime = _runtime(request, backend)
    answer = runtime.query({"select": [NET_MEDIAN], "time": MONTH, "where": [REPEAT]}, now=NOW)
    medians = {
        str(row["period"])[:10]: None if row["median_net"] is None else float(row["median_net"])
        for row in answer["rows"]
    }
    assert medians == {
        "2023-11-01": 4.5,
        "2023-12-01": 7.0,
        "2024-01-01": None,
        "2024-03-01": 0.0,
        "2024-05-01": 6.0,
        "2024-06-01": 3.0,
    }


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_bounded_window_reads_only_the_orders_with_rows_in_each_month(request, backend):
    """Grouped by order, a bounded window fills every order into every month (0); a
    distribution reads only the orders with rows there, so each month is its one order."""
    runtime = _runtime(request, backend)
    window = {**MONTH, "start": "2024-06-01", "end": "2024-09-01"}
    by_order = runtime.query(
        {"select": [{"expression": NET, "as": "net"}], "group_by": ["dimension.shop_order_id"],
         "time": window},
        now=NOW,
    )  # fmt: skip
    assert len(by_order["rows"]) == 9
    answer = runtime.query({"select": [NET_MEDIAN], "time": window}, now=NOW)
    assert [float(row["median_net"]) for row in answer["rows"]] == [3.0, 9.0, 2.0]


# Store b's sales with no ELSE: an order at another store has no value here, so a customer
# whose orders are all at other stores reads store b sales 0 grouped by customer, data of
# nothing. The shop package declares no such sum, so these tests add one to a copy of it.
STORE_B_SALES = {
    "label": "Store b sales",
    "kind": "aggregate",
    "accumulation": {"kind": "flow"},
    "value_type": "currency",
    "expr": {
        "kind": "case",
        "whens": [
            {
                "when": {
                    "kind": "comparison",
                    "op": "=",
                    "left": {"kind": "column", "column": "store_id"},
                    "right": {"kind": "literal", "value": "b"},
                },
                "then": {"kind": "column", "column": "amount"},
            }
        ],
    },
}
EXCLUDING_B = {
    "kind": "arithmetic",
    "op": "subtract",
    "left": "measure.shop.revenue",
    "right": "measure.shop.store_b_sales",
}
# Each customer's revenue less their store b sales, 0 for a customer with no store b order
# (store b has rows in scope), over the raw orders.
EXCLUDING_B_SQL = """
SELECT customer_id,
  SUM(amount) - CASE WHEN COUNT(CASE WHEN store_id = 'b' THEN 1 END) = 0 THEN 0
    ELSE SUM(CASE WHEN store_id = 'b' THEN amount END) END AS excluding_b
FROM orders GROUP BY customer_id
"""
REPEAT_SQL = "HAVING COUNT(DISTINCT order_id) >= 2"


def _customer_median(input_: object) -> dict[str, object]:
    return {
        "expression": {
            "kind": "distribution",
            "function": "median",
            "over": {"kind": "entity_value", "entity": "entity.shop_customer", "input": input_},
        },
        "as": "median",
    }


@pytest.fixture(scope="module", params=["duckdb", "postgres"])
def store_b(request, tmp_path_factory) -> Iterator[Runtime]:
    """The shop package with store b sales declared, on DuckDB or in a Postgres schema of its
    own (dropped afterwards)."""
    package = _write_variant(tmp_path_factory.mktemp("store_b"), "utc_authored")
    orders = package / "models" / "orders.yml"
    model = yaml.safe_load(orders.read_text(encoding="utf-8"))
    model["model"]["measures"]["store_b_sales"] = STORE_B_SALES
    orders.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
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
    schema = f"sr_store_b_{uuid.uuid4().hex[:12]}"
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
        _rows(runtime, SEED)
        yield runtime
    finally:
        runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def _median_of(runtime: Runtime, call: dict[str, object]) -> float:
    (row,) = runtime.query(call, now=NOW)["rows"]
    return float(row["median"])


def _hand_median(runtime: Runtime, having: str = "") -> float:
    (row,) = _rows(
        runtime,
        f"WITH per_customer AS ({EXCLUDING_B_SQL} {having}) "
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY excluding_b) FROM per_customer",
    )
    return float(row[0])


def test_revenue_excluding_store_b_reads_a_customer_with_no_store_b_order_at_their_revenue(
    store_b: Runtime,
):
    """Store b sales and revenue read the same order rows, but store b sales has no ELSE, so a
    customer with no store b order has no value of it unless it settles: beside revenue it does,
    as grouped by customer. Repeat customers 101 (23 less 0), 102 (9 less 5) and 103 (23 less
    3) read 23, 4 and 20, so 20; with 101 left out it read 12."""
    by_customer = store_b.query(
        {"select": [{"expression": EXCLUDING_B, "as": "v"}],
         "group_by": ["dimension.shop_customer_id"], "where": [REPEAT]},
        now=NOW,
    )  # fmt: skip
    values = {row["dimension.shop_customer_id"]: float(row["v"]) for row in by_customer["rows"]}
    assert values == {101: 23.0, 102: 4.0, 103: 20.0}

    repeat = {"select": [_customer_median(EXCLUDING_B)], "where": [REPEAT]}
    assert _median_of(store_b, repeat) == _hand_median(store_b, REPEAT_SQL) == 20.0
    # Every customer with a known revenue: 101 (23), 102 (4), 103 (20), 104 and 106 (store b
    # only, 0) and 108 (2); 105's only order has no amount. The median of six is 3.
    every = {"select": [_customer_median(EXCLUDING_B)]}
    assert _median_of(store_b, every) == _hand_median(store_b) == 3.0
    # Repeat web customers, 101 and 103, read 23 and 20 under the query scope; under dataset
    # the where beside the entity filter can't be settled, as for net order value.
    web = {**repeat, "where": [REPEAT, WEB]}
    assert _median_of(store_b, {**web, "observation_scope": "query"}) == 21.5
    with pytest.raises(SemanticLayerError) as raised:
        store_b.query(web, now=NOW)
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"


def test_store_b_sales_alone_reads_the_customers_it_has_a_value_for(store_b: Runtime):
    """Alone, a distribution reads the entities its one measure has a value for, as its filtered
    twin does: customers 102 (5), 103 (3), 104 (8) and 106 (9), so 6.5 on both."""
    filtered = {
        "kind": "aggregate",
        "measure": "measure.shop.revenue",
        "filter": {"all": [{"field": "dimension.shop_order_store_id", "op": "is", "value": "b"}]},
    }
    alone = _median_of(store_b, {"select": [_customer_median("measure.shop.store_b_sales")]})
    assert alone == _median_of(store_b, {"select": [_customer_median(filtered)]}) == 6.5
