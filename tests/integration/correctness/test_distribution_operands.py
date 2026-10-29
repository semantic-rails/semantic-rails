"""A distribution over several measures settles each entity's operands before it aggregates.

"Median net order value" is revenue less goods refunds per order: an order with no refund row
refunded nothing (0) where refunds have data in scope, and the ledger pins its answers
(shop/median-net-order-value-*). Here: the refusal a user sees where the per-entity values
can't be settled, and the entities a bounded window reads, on DuckDB and Postgres.
"""

from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError

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
