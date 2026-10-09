"""Every jaffle question an entity grouping makes ready, checked against plain SQL.

"store", "stores", "each store" and "top 3 stores" name the Store entity, so the answer groups
by its key and its one naming dimension, Store name; "order" groups by the order key. Each case
below was held before. Its draft must plan ``ok`` with that grouping and return exactly the rows
of a DuckDB query written from the seed tables, not from the engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

STORE_ID = "dimension.jaffle_store_id"
STORE = "dimension.jaffle_store_name"
KEYED = (STORE_ID, STORE)
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER = "dimension.jaffle_order_id"
CUSTOMER = "dimension.jaffle_customer_id"
ITEM_TYPE = "dimension.jaffle_item_product_type"
SKU = "dimension.jaffle_product_sku"
COLUMNS = {
    STORE_ID: "s.store_id",
    STORE: "s.store_name",
    CUSTOMER_TYPE: "c.customer_type",
    ORDER: "o.order_id",
    CUSTOMER: "o.customer_id",
    ITEM_TYPE: "i.product_type",
    SKU: "i.sku",
}
ORDERS_FROM = (
    "jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
    "LEFT JOIN jaffle_customer c ON o.customer_id = c.customer_id"
)
ITEMS_FROM = (
    "jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
    "JOIN jaffle_store s ON o.store_id = s.store_id"
)
SESSIONS_FROM = "jaffle_storefront_session x JOIN jaffle_store s ON x.store_id = s.store_id"
REVENUE = "SUM(o.order_total_cents / 100.0)"
ORDERS = "COUNT(DISTINCT o.order_id)"
AOV = f"{REVENUE} / {ORDERS}"
ITEM_REVENUE = "SUM(i.item_revenue_cents / 100.0)"
BOTH = "s.store_name IN ('Brooklyn', 'Philadelphia')"
BOTH_PARTIAL = {"where": [{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}]}
AUGUST = "2017-08-21"  # "last month" is July 2017, "last week" 2017-08-14 .. 2017-08-21
JULY = ("2017-07-01", "2017-08-01")
YEAR_2017 = ("2017-01-01", "2018-01-01")


@dataclass(frozen=True)
class _Case:
    question: str
    values: tuple[str, ...] = (REVENUE,)
    dims: tuple[str, ...] = KEYED
    grain: str = ""
    window: tuple[str, str] | None = None
    where: str = ""
    source: str = ORDERS_FROM
    limit: int = 0
    now: str = ""
    partial: dict[str, Any] | None = None

    def sql(self) -> str:
        bucket = [f"DATE_TRUNC('{self.grain}', o.ordered_at)"] if self.grain else []
        columns = [*(COLUMNS[dim] for dim in self.dims), *bucket, *self.values]
        window = (
            [f"o.ordered_at >= TIMESTAMP '{self.window[0]}'"]
            + [f"o.ordered_at < TIMESTAMP '{self.window[1]}'"]
            if self.window
            else []
        )
        conditions = [*([self.where] if self.where else []), *window]
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        ranked = f" ORDER BY {len(columns)} DESC LIMIT {self.limit}" if self.limit else ""
        return f"SELECT {', '.join(columns)} FROM {self.source}{where} GROUP BY ALL{ranked}"

    def __str__(self) -> str:
        return self.question + (f" {self.partial}" if self.partial else "")


CASES = [
    # By store, over all time.
    _Case("revenue by store"),
    _Case("revenue by store", partial={}),
    _Case("revenue by store", partial={"where": []}),
    _Case("revenue by stores"),
    _Case("List revenue by store", now=AUGUST),
    _Case("revenue for each store"),
    _Case("revenue at store name level for each store"),
    _Case("revenue at store name level for each store", where=BOTH, partial=BOTH_PARTIAL),
    _Case(
        "revenue by store name for each store",
        dims=(STORE, STORE_ID),
        where=BOTH,
        partial=BOTH_PARTIAL,
    ),
    _Case("revenue by store and customer type", dims=(*KEYED, CUSTOMER_TYPE)),
    _Case(
        "Show total revenue by store where customer type is repeat.",
        where="c.customer_type = 'repeat'",
    ),
    _Case("orders by store", (ORDERS,)),
    _Case("orders by store", (ORDERS,), partial={}),
    _Case("orders by store in Brooklyn", (ORDERS,), where="s.store_name = 'Brooklyn'"),
    _Case("average order value by store", (AOV,)),
    _Case("drink revenue by store", ("SUM(o.drink_revenue_cents / 100.0)",)),
    _Case(
        "repeat customer orders by store",
        ("COUNT(DISTINCT o.order_id) FILTER (WHERE c.lifetime_order_count > 1)",),
    ),
    _Case(
        "gross margin percentage by store",
        (
            "(SUM(o.subtotal_cents / 100.0) - SUM(o.order_cost_cents / 100.0)) "
            "/ SUM(o.subtotal_cents / 100.0)",
        ),
    ),
    _Case(
        "item revenue for food from Brooklyn by store",
        (ITEM_REVENUE,),
        where="s.store_name = 'Brooklyn' AND i.product_type = 'jaffle'",
        source=ITEMS_FROM,
    ),
    # The caller's grouping or select settles how stores show.
    _Case(
        "revenue by store",
        ("AVG(o.order_total_cents / 100.0)", REVENUE),
        partial={
            "select": [
                {
                    "as": "avg_rev",
                    "expression": {"aggregation": "avg", "measure": "measure.jaffle.revenue_usd"},
                }
            ]
        },
    ),
    _Case(
        "revenue by store",
        (AOV, REVENUE),
        dims=(STORE,),
        partial={
            "group_by": [STORE],
            "select": [{"expression": {"kind": "metric", "metric": "metric.sales.aov_usd"}}],
        },
    ),
    _Case(
        "revenue by store",
        (AOV, REVENUE),
        dims=(STORE,),
        partial={
            "select": [
                {"metric": "metric.sales.aov_usd"},
                {"expression": {"dimension": STORE}},
            ]
        },
    ),
    _Case("revenue by store", dims=(STORE,), partial={"group_by": [{"dimension": STORE}]}),
    _Case(
        "item revenue by store",
        (ITEM_REVENUE,),
        dims=(ITEM_TYPE, STORE),
        source=ITEMS_FROM,
        partial={"group_by": [ITEM_TYPE, STORE]},
    ),
    # By store, bucketed by a grain over all time.
    *(
        _Case(question, grain="month")
        for question in [
            "monthly revenue by store",
            "show monthly revenue by store",
            "revenue by store in each month",
            "revenue by store, at month grain",
            "revenue by store, at month level",
            "revenue by store, month level",
        ]
    ),
    _Case("orders by store and month", (ORDERS,), grain="month"),
    *(
        _Case(f"revenue by store, {spelling}", grain=grain)
        for grain in ("day", "week", "quarter", "year")
        for spelling in (f"at {grain} grain", f"at {grain} level", f"{grain} level")
    ),
    # By store, in a window.
    _Case("monthly revenue by store for 2017", grain="month", window=YEAR_2017),
    _Case(
        "monthly revenue by store from January 2017 through June 2017",
        grain="month",
        window=("2017-01-01", "2017-07-01"),
    ),
    _Case(
        "monthly revenue for the last 3 months by store",
        grain="month",
        window=("2017-05-01", "2017-08-01"),
        now=AUGUST,
    ),
    _Case("revenue by store for March 2017", grain="month", window=("2017-03-01", "2017-04-01")),
    _Case(
        "revenue by store from January 1 2016 to December 31 2017 by year",
        grain="year",
        window=("2016-01-01", "2018-01-01"),
    ),
    _Case("revenue in Q1 2017 by store", grain="quarter", window=("2017-01-01", "2017-04-01")),
    _Case("revenue by store in Q2 2017", grain="quarter", window=("2017-04-01", "2017-07-01")),
    *(
        _Case(question, grain="month", window=JULY, now=AUGUST)
        for question in [
            "revenue by store last month",
            "revenue last month by store",
            "revenue by store, last month",
            "Revenue by store last month",
            "Revenue for each store last month",
        ]
    ),
    _Case(
        "revenue by store last month and customer type",
        dims=(*KEYED, CUSTOMER_TYPE),
        grain="month",
        window=JULY,
        now=AUGUST,
    ),
    _Case(
        "average order value by store in Q2 2017 for Philadelphia and Brooklyn",
        (AOV,),
        grain="quarter",
        window=("2017-04-01", "2017-07-01"),
        where=BOTH,
    ),
    _Case(
        "orders on 15 March 2017 by store",
        (ORDERS,),
        grain="day",
        window=("2017-03-15", "2017-03-16"),
    ),
    *(
        _Case(question, (ORDERS,), grain="week", window=("2017-08-14", "2017-08-21"), now=AUGUST)
        for question in [
            "Show orders by store last week.",
            "How many orders did each store get last week?",
        ]
    ),
    _Case(
        "Revenue and order count by store last quarter",
        (REVENUE, ORDERS),
        grain="quarter",
        window=("2017-01-01", "2017-04-01"),
        now="2017-04-15",
    ),
    # Rankings of stores.
    *(
        _Case(question, limit=limit)
        for question, limit in [
            ("top 1 store by revenue", 1),
            ("top 3 stores by revenue", 3),
            ("top 5 stores by revenue", 5),
            ("the top 5 stores by revenue", 5),
            ("top five stores by revenue", 5),
        ]
    ),
    *(
        _Case("top 3 stores by revenue", limit=3, partial={"select": [select]})
        for select in [
            {
                "as": "rev",
                "expression": {"aggregation": "sum", "measure": "measure.jaffle.revenue_usd"},
            },
            {"as": "rev", "expression": {"measure": "measure.jaffle.revenue_usd"}},
            {"expression": {"measure": "measure.jaffle.revenue_usd"}},
        ]
    ),
    _Case("top 5 stores by revenue in 2017", grain="year", window=YEAR_2017, limit=5),
    _Case("top 3 stores by orders", (ORDERS,), limit=3),
    _Case("which 5 stores had the most orders", (ORDERS,), limit=5),
    _Case(
        "which store had the most orders in 2017",
        (ORDERS,),
        grain="year",
        window=YEAR_2017,
        limit=1,
    ),
    _Case(
        "Which 3 stores had the most revenue last month?",
        grain="month",
        window=JULY,
        limit=3,
        now=AUGUST,
    ),
    _Case(
        "which store had the most customers",
        ("COUNT(DISTINCT x.customer_id)",),
        source=SESSIONS_FROM,
        limit=1,
    ),
    # "each" and "every" name a dimension outright.
    *(
        _Case(question, dims=(CUSTOMER_TYPE,))
        for question in [
            "revenue each customer type",
            "revenue every customer type",
            "revenue for each customer type",
        ]
    ),
    *(
        _Case(question, dims=(STORE,), where=BOTH, partial=BOTH_PARTIAL)
        for question in [
            "revenue each store name",
            "revenue every store name",
            "revenue for each store name",
        ]
    ),
    # A list: the customers with an order in the window, one row each.
    _Case(
        "Which customers ordered last week?",
        (ORDERS,),
        dims=(CUSTOMER,),
        grain="week",
        window=("2017-08-14", "2017-08-21"),
        now=AUGUST,
    ),
    # Other entities: an order, a product.
    _Case("revenue by order", dims=(ORDER,)),
    _Case("item revenue by orders", (ITEM_REVENUE,), dims=(ORDER,), source=ITEMS_FROM),
    _Case(
        "item revenue by product type and product",
        (ITEM_REVENUE,),
        dims=(ITEM_TYPE, SKU),
        source=ITEMS_FROM,
    ),
]


@pytest.fixture(scope="module")
def jaffle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    root = copy_package_config(tmp_path_factory.mktemp("jaffle"), "jaffle_shop", preseed_db=True)
    runtime = opened(Runtime.from_path(str(root)))
    yield runtime
    runtime.close()


def _context(case: _Case) -> dict[str, Any]:
    return {"policy_context": {"now": case.now}} if case.now else {}


@pytest.mark.parametrize("case", CASES, ids=str)
def test_an_entity_grouping_answers_like_plain_sql(jaffle: Runtime, case: _Case) -> None:
    partial = {**(case.partial or {}), **_context(case)}
    payload = plan_payload(jaffle, intent=case.question, partial_query=partial or None)
    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query_ir"]
    assert query.get("group_by") == list(case.dims)
    assert query.get("limit") == (case.limit or None)
    rows = jaffle.query({**query, **_context(case)})["rows"]
    with duckdb.connect(str(Path(jaffle.db_path)), read_only=True) as connection:
        reference = connection.execute(case.sql()).fetchall()
    assert reference, case.sql()
    dims, keys = len(case.dims), len(case.dims) + bool(case.grain)
    bucket = next((key for key in rows[0] if key.startswith("temporal_role.")), "") if rows else ""
    values = [key for key in (rows[0] if rows else {}) if key not in case.dims and key != bucket]
    actual = [
        (
            *(str(row[dim]) for dim in case.dims),
            *([str(row[bucket])[:10]] if case.grain else []),
            *(float(row[key]) for key in values),
        )
        for row in rows
    ]
    expected = [
        (
            *(str(value) for value in row[:dims]),
            *([str(row[dims])[:10]] if case.grain else []),
            *(float(value) for value in row[keys:]),
        )
        for row in reference
    ]
    if not case.limit:
        actual, expected = sorted(actual), sorted(expected)
    assert [row[:keys] for row in actual] == [row[:keys] for row in expected]
    assert [value for row in actual for value in row[keys:]] == pytest.approx(
        [value for row in expected for value in row[keys:]]
    )


@pytest.mark.parametrize(
    "question",
    [
        "top 5 customers",
        "top 5 stores",
        "the 5 customers who spent the most",
        "which 5 customers spent the most",
        "the 3 stores that sold the most",
    ],
)
def test_a_ranking_that_names_only_its_rows_is_held(jaffle: Runtime, question: str) -> None:
    # The only word naming a subject is the ranked noun: ranking customers by their own count
    # ranks every row at one, so plan holds the ranking rather than answer it.
    payload = plan_payload(jaffle, intent=question)
    assert payload["status"] != "ok"
    assert "execute" not in payload["next"].get("ready_for", [])
