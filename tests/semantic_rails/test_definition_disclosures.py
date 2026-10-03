"""Say when a row mixes facts on different clocks, and what an average averages over.

Both answers are right but read as something else. Orders and storefront sessions grouped by
customer, with no window, each count all of their own history, so a ratio of the two is not a
rate over one period: the answer carries ``MIXED_TIME_ROLES``. An ``avg`` of item revenue grouped
by customer averages item rows, not orders: the answer's ``assumptions`` say so and give the
per-order average as a ratio. Neither changes the numbers.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.runtime import Runtime

CUSTOMER = "dimension.jaffle_customer_id"
STORE = "dimension.jaffle_store_name"
ORDER_TIME = "temporal_role.jaffle_order_time"
SESSION_TIME = "temporal_role.jaffle_session_started_at"
DELIVERED_TIME = "temporal_role.jaffle_lifecycle_delivered_at"
# A governed metric over two clocks: the package defined it, so it is one clock.
ORDERS_PER_SESSION = "metric.test.orders_per_session"
_ORDERS_PER_SESSION_YAML = """\
metrics:
  test.orders_per_session:
    as: metric.test.orders_per_session
    label: Orders per session
    description: Orders over storefront sessions, each on its own clock.
    kind: ratio
    numerator: order_count
    denominator: session_starts
"""
ITEM_REVENUE_BY_ORDER = (
    '{"kind":"ratio","numerator":{"kind":"aggregate","measure":"measure.jaffle.item_revenue_usd",'
    '"aggregation":"sum"},"denominator":{"kind":"aggregate","measure":"measure.jaffle.order_count"}}'
)
REVENUE_BY_CUSTOMER = (
    '{"kind":"ratio","numerator":{"kind":"aggregate","measure":"measure.jaffle.revenue_usd",'
    '"aggregation":"sum"},"denominator":{"kind":"aggregate",'
    '"measure":"measure.jaffle.customer_count"}}'
)


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from tests.semantic_rails.conftest import copy_package_config

    package_dir = copy_package_config(
        tmp_path_factory.mktemp("disclosures"), "jaffle_shop", preseed_db=True
    )
    (package_dir / "metrics" / "extensions" / "orders_per_session.yml").write_text(
        _ORDERS_PER_SESSION_YAML, encoding="utf-8"
    )
    runtime = Runtime.from_path(str(package_dir))
    yield runtime
    runtime.close()


def _agg(measure: str, aggregation: str = "") -> dict[str, Any]:
    out = {"kind": "aggregate", "measure": f"measure.jaffle.{measure}"}
    return {**out, "aggregation": aggregation} if aggregation else out


def _query(*selects: dict[str, Any], group_by=(CUSTOMER,), **extra: Any) -> dict[str, Any]:
    return {
        "version": 2,
        "select": [{"expression": expr, "as": f"v{index}"} for index, expr in enumerate(selects)],
        "group_by": list(group_by),
        **extra,
    }


def _mixed(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in out["warnings"] if w["code"] == "MIXED_TIME_ROLES"]


ORDERS, SESSIONS = _agg("order_count"), _agg("session_starts")
SESSION_WINDOW = {"temporal_role": SESSION_TIME, "grain": "month"}
SESSIONS_IF = {
    "kind": "aggregate_if",
    "aggregation": "count",
    "condition": {
        "kind": "comparison",
        "op": "=",
        "left": {
            "kind": "column",
            "column": "store_id",
            "entity": "entity.jaffle_storefront_session",
        },
        "right": {"kind": "literal", "value": "1"},
    },
}
FOOD_ITEMS_IF_AVG = {
    "kind": "aggregate_if",
    "aggregation": "avg",
    "condition": {
        "kind": "comparison",
        "op": "=",
        "left": {"kind": "column", "column": "product_type", "entity": "entity.jaffle_item"},
        "right": {"kind": "literal", "value": "jaffle"},
    },
    "value": {"kind": "column", "column": "item_revenue_cents", "entity": "entity.jaffle_item"},
}


@pytest.mark.parametrize("method", ["validate", "compile", "query"])
def test_two_facts_on_different_clocks_with_no_window_get_one_warning(runtime, method):
    out = getattr(runtime, method)(_query(ORDERS, SESSIONS))

    [warning] = _mixed(out)
    assert warning["severity"] == "warning"
    assert warning["message"] == (
        "These measures are dated by different time roles: measure.jaffle.order_count by "
        f"{ORDER_TIME}; measure.jaffle.session_starts by {SESSION_TIME}. With no window each "
        "covers all of its own history; add a window, or read them separately."
    )
    assert warning["details"] == {
        "clocks": [
            {"subject": "measure.jaffle.order_count", "temporal_roles": [ORDER_TIME]},
            {"subject": "measure.jaffle.session_starts", "temporal_roles": [SESSION_TIME]},
        ]
    }
    assert warning["object_ids"] == ["measure.jaffle.order_count", "measure.jaffle.session_starts"]


@pytest.mark.parametrize(
    "query",
    [
        # A window covers one period on each fact's own clock.
        _query(
            ORDERS, SESSIONS, time={**SESSION_WINDOW, "start": "2016-09-01", "end": "2016-10-01"}
        ),
        # With a time grain each row is one period, not all of history.
        _query(ORDERS, SESSIONS, time=SESSION_WINDOW),
        # Items and orders share the order's clock.
        _query(_agg("item_revenue_usd"), ORDERS),
        # One fact.
        _query(ORDERS, _agg("revenue_usd")),
        # A governed metric over two clocks, alone, is the package's own definition.
        _query({"metric": ORDERS_PER_SESSION}),
    ],
    ids=["window", "grain", "shared-role", "one-fact", "governed-metric"],
)
def test_no_mixed_clock_warning(runtime, query):
    assert _mixed(runtime.query(query)) == []


@pytest.mark.parametrize(
    ("selects", "clocks"),
    [
        # Inside one select expression, as a ratio or as arithmetic.
        (
            [{"kind": "ratio", "numerator": ORDERS, "denominator": SESSIONS}],
            ["measure.jaffle.order_count by", "measure.jaffle.session_starts by"],
        ),
        (
            [{"kind": "arithmetic", "op": "subtract", "left": ORDERS, "right": SESSIONS}],
            ["measure.jaffle.order_count by", "measure.jaffle.session_starts by"],
        ),
        # An aggregate_if over sessions has no time role, so it is its own clock.
        ([ORDERS, SESSIONS_IF], ["aggregate_if(count, …) has no time role"]),
        # A governed metric is one clock, with every role it combines.
        (
            [{"metric": ORDERS_PER_SESSION}, _agg("delivered_orders")],
            [
                f"{ORDERS_PER_SESSION} by {ORDER_TIME} and {SESSION_TIME}",
                f"measure.jaffle.delivered_orders by {DELIVERED_TIME}",
            ],
        ),
    ],
    ids=["ratio", "arithmetic", "aggregate-if", "governed-metric"],
)
def test_measures_inside_a_select_expression_are_disclosed_too(runtime, selects, clocks):
    [warning] = _mixed(runtime.query(_query(*selects)))

    assert all(clock in warning["message"] for clock in clocks)


def test_an_average_of_item_rows_by_customer_names_them_and_the_per_order_ratio(runtime):
    out = runtime.query(_query(_agg("item_revenue_usd", "avg")))

    assert out["assumptions"] == [
        "avg(measure.jaffle.item_revenue_usd) averages over Item rows; for a per-Order average "
        f"select {ITEM_REVENUE_BY_ORDER}."
    ]
    # Minimal responses keep it: it changes what the number means.
    minimal = runtime.query({**_query(_agg("item_revenue_usd", "avg")), "verbosity": "minimal"})
    assert minimal["assumptions"] == out["assumptions"]


def test_the_hinted_ratio_is_the_per_order_average(runtime):
    import json

    hinted = runtime.query(_query(json.loads(ITEM_REVENUE_BY_ORDER)))
    independent = runtime._get_adapter().query(
        "SELECT o.customer_id, AVG(COALESCE(t.item_total, 0)) AS per_order "
        "FROM jaffle_order AS o LEFT JOIN ("
        "SELECT order_id, SUM(item_revenue_cents) / 100.0 AS item_total "
        "FROM jaffle_item GROUP BY order_id) AS t ON t.order_id = o.order_id "
        "GROUP BY o.customer_id"
    )

    expected = {row["customer_id"]: row["per_order"] for row in independent}
    answered = {row[CUSTOMER]: row["v0"] for row in hinted["rows"] if row["v0"] is not None}
    assert len(expected) > 100
    assert answered == pytest.approx(expected)
    # The item-row average differs from it.
    averaged = runtime.query(_query(_agg("item_revenue_usd", "avg")))
    item_rows = {row[CUSTOMER]: row["v0"] for row in averaged["rows"] if row["v0"] is not None}
    assert item_rows != pytest.approx(expected)


@pytest.mark.parametrize(
    ("selects", "group_by", "assumptions"),
    [
        # Inside arithmetic: the same entry.
        (
            [
                {
                    "kind": "arithmetic",
                    "op": "multiply",
                    "left": _agg("item_revenue_usd", "avg"),
                    "right": {"kind": "literal", "value": 100},
                }
            ],
            [CUSTOMER],
            [
                f"avg(measure.jaffle.item_revenue_usd) averages over Item rows; for a per-Order "
                f"average select {ITEM_REVENUE_BY_ORDER}."
            ],
        ),
        # An aggregate_if has no measure id to sum, so it states the grain only.
        ([FOOD_ITEMS_IF_AVG], [CUSTOMER], ["aggregate_if(avg, …) averages over Item rows."]),
        # A maximum is taken over item rows; a per-order maximum is no ratio.
        (
            [_agg("item_revenue_usd", "max")],
            [CUSTOMER],
            ["max(measure.jaffle.item_revenue_usd) is taken over Item rows."],
        ),
        # With no grouping, each declared parent with a count of its key gets the ratio.
        (
            [_agg("item_revenue_usd", "avg")],
            [],
            [
                f"avg(measure.jaffle.item_revenue_usd) averages over Item rows; for a per-Order "
                f"average select {ITEM_REVENUE_BY_ORDER}."
            ],
        ),
        # Customers have no parent.
        ([_agg("lifetime_spend_before_tax_usd", "avg")], [], []),
        # Nothing between orders and stores.
        ([_agg("revenue_usd", "avg")], [STORE], []),
        # A sum needs no disclosure.
        ([_agg("item_revenue_usd")], [CUSTOMER], []),
    ],
    ids=["arithmetic", "aggregate-if", "max", "no-grouping", "no-parent", "direct-parent", "sum"],
)
def test_averaging_grain(runtime, selects, group_by, assumptions):
    assert runtime.query(_query(*selects, group_by=group_by))["assumptions"] == assumptions


@pytest.mark.parametrize(
    ("time", "assumptions"),
    [
        # With no query time, every customer counts.
        (
            {},
            [
                "avg(measure.jaffle.revenue_usd) averages over Order rows; for a per-Customer "
                f"average select {REVENUE_BY_CUSTOMER}."
            ],
        ),
        # Customers are counted by their first order, not by the order time this query reads.
        (
            {
                "time": {
                    "temporal_role": ORDER_TIME,
                    "grain": "year",
                    "start": "2017-01-01",
                    "end": "2018-01-01",
                }
            },
            ["avg(measure.jaffle.revenue_usd) averages over Order rows."],
        ),
    ],
    ids=["no-time", "order-time"],
)
def test_a_per_parent_ratio_counts_parents_on_the_querys_time_role(runtime, time, assumptions):
    out = runtime.query(_query(_agg("revenue_usd", "avg"), group_by=[], **time))

    assert out["assumptions"] == assumptions


def test_disclosures_never_change_the_answer(runtime, monkeypatch):
    queries = [_query(ORDERS, SESSIONS), _query(_agg("item_revenue_usd", "avg"))]
    disclosed = [runtime.query(query) for query in queries]
    assert [len(_mixed(disclosed[0])), len(disclosed[1]["assumptions"])] == [1, 1]
    monkeypatch.setattr(runtime_module, "mixed_time_role_warnings", lambda *_: [])
    monkeypatch.setattr(runtime_module, "averaging_grain_assumptions", lambda *_: [])

    for query, before in zip(queries, disclosed, strict=True):
        after = runtime.query(query)
        assert _mixed(after) == [] and after["assumptions"] == []
        assert after["rendered_sql"] == before["rendered_sql"]
        assert _by_customer(after["rows"]) == _by_customer(before["rows"])


def _by_customer(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row[CUSTOMER]))
