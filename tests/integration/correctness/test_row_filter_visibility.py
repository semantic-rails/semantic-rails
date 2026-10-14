"""Row filters hold through joins, routes and entity hops: a filtered caller's answer is an
unfiltered caller's answer on the rows that caller can see.

The shop package's regional policy filters stores by the caller's trusted ``region``. An order
is visible when its store is, and a refund, scan or tag link when its order is; an order with
no store, or a scan with no order or an unknown one, is visible to no filtered caller.
Customers and tags hold no store, so no region reaches them: they are looked up, never
counted or listed, under the filter.

Every check reads numbers through the public query surfaces, on DuckDB:

- the rule: a West manager's answer on the shop data equals an unfiltered caller's answer on
  that data with every row the West can't see deleted;
- hidden-only changes (a hidden store sharing a visible store's key, hidden rows rescaled,
  added or renamed) change nothing in a West manager's response;
- a question the policy can't answer yet is held with its reason, never answered;
- the store picker lists only stores the caller can see.

Beside the ledger's West cases, the extra queries read a restricted relation in more than one
place (an entity filter, a condition on a child, a prior period, a ratio's two leaves, a
three-table join, a filter value check, a coverage read), so a place read without the caller's
visibility shows as a wrong number, or as a response a hidden-only change moves.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails import operations
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

from .answer_ledger import call_arguments, comparable, encode, load_entries
from .conftest import SESSION_ZONE, _rows, _runtime, _write_variant

WEST = RequestContext(roles=("regional",), attributes={"region": "West"})
EAST = RequestContext(roles=("regional",), attributes={"region": "East"})
REVENUE = "measure.shop.revenue"
ORDERED_AT = "temporal_role.shop_order_ordered_at"
STORE_NAME = "dimension.shop_store_name"
REVENUE_BY_STORE = {"select": [REVENUE], "group_by": [STORE_NAME]}

# The rollup is built from orders, so a change to orders rebuilds it.
REBUILD_ROLLUP = """
DELETE FROM orders_monthly;
INSERT INTO orders_monthly
SELECT CAST(date_trunc('month', ordered_at) AS DATE), store_id, SUM(amount), COUNT(order_id)
FROM orders GROUP BY 1, 2;
"""
# The rows a West manager can see, and nothing else: Oak and its orders go, and so do order 8
# (no store), the refunds, scans and tag links of hidden orders, and the scans with no order
# or an unknown one. Customers, tags and every other table stay whole.
VISIBLE_TO_WEST = (
    """
DELETE FROM stores WHERE region IS DISTINCT FROM 'West';
DELETE FROM orders WHERE store_id IS NULL OR store_id NOT IN (SELECT store_id FROM stores);
DELETE FROM refunds WHERE order_id IS NULL OR order_id NOT IN (SELECT order_id FROM orders);
DELETE FROM shipment_scans
WHERE order_id IS NULL OR order_id NOT IN (SELECT order_id FROM orders);
DELETE FROM order_tags WHERE order_id IS NULL OR order_id NOT IN (SELECT order_id FROM orders);
"""
    + REBUILD_ROLLUP
)
# An East store row that shares Elm's key: a key match no longer proves a store is visible.
HIDDEN_DUPLICATE_KEY = "\nINSERT INTO stores VALUES ('a', 'Secret', 'East');\n"
# Every change touches only rows a West manager can't see: the duplicate key, Oak renamed,
# hidden amounts rescaled, and a later hidden order (it moves an unfiltered caller's data edge)
# with a refund, a scan and a tag link of hidden orders, plus another scan with no order.
HIDDEN_ROWS_CHANGED = (
    HIDDEN_DUPLICATE_KEY
    + """
UPDATE stores SET name = 'Oakley' WHERE store_id = 'b';
UPDATE orders SET amount = amount * 100 WHERE store_id = 'b' OR store_id IS NULL;
INSERT INTO orders (order_id, customer_id, store_id, ordered_at, order_date, amount)
VALUES (12, 107, 'b', TIMESTAMP '2024-09-15 12:00:00', DATE '2024-09-15', 50.00);
UPDATE orders SET ordered_at_tz = ordered_at AT TIME ZONE 'UTC' WHERE order_id = 12;
INSERT INTO refunds VALUES (7, 2, 'goods', 9.00, NULL, NULL);
INSERT INTO shipment_scans VALUES
  (12, TIMESTAMP '2024-09-16 12:00:00', 'pickup', 9.00),
  (NULL, TIMESTAMP '2024-09-17 12:00:00', 'delivery', 9.00);
INSERT INTO order_tags VALUES (12, 1), (2, 3);
"""
    + REBUILD_ROLLUP
)

# Beside the ledger's cases: each reads a restricted relation in a second place.
EVERY_OCCURRENCE: dict[str, tuple[dict[str, Any], str | None]] = {
    # The entity filter reads orders on its own: with every order, customer 101 (23) would pass.
    "customers-with-20-of-visible-revenue": (
        {
            "select": [REVENUE],
            "where": [
                {
                    "entity": "entity.shop_customer",
                    "having": [{"field": REVENUE, "op": "gte", "value": 20}],
                    "over": "all_time",
                }
            ],
        },
        None,
    ),
    # A condition on a child reads refunds inside an existence check.
    "orders-with-a-goods-refund": (
        {
            "select": ["measure.shop.order_count"],
            "where": [{"field": "dimension.shop_refund_refund_type", "op": "is", "value": "goods"}],
        },
        None,
    ),
    # The month before reads orders a second time, over the calendar.
    "monthly-revenue-and-the-month-before": (
        {
            "select": [
                REVENUE,
                {
                    "expression": {
                        "kind": "prior_period",
                        "input": REVENUE,
                        "offset": {"unit": "month", "value": 1},
                    },
                    "as": "month_before",
                },
            ],
            "time": {"temporal_role": ORDERED_AT, "grain": "month", "end": "2024-09-01"},
        },
        "2024-09-01T00:00:00Z",
    ),
    # A ratio reads its numerator and denominator in separate leaves.
    "average-order-value": ({"select": ["metric.shop.average_order_value"]}, None),
    # Refunds, their orders and the orders' stores: three restricted reads in one join.
    "goods-refunded-by-store": (
        {"select": ["measure.shop.goods_refunded"], "group_by": [STORE_NAME]},
        None,
    ),
    # The filter value check reads store names: a hidden store is no store at all.
    "revenue-at-a-hidden-store": (
        {"select": [REVENUE], "where": [{"field": STORE_NAME, "op": "is", "value": "Oak"}]},
        None,
    ),
    # Coverage reads the data's edge: the West's last order is August 1, whoever ordered later.
    "monthly-revenue-past-the-west-edge": (
        {
            "select": [REVENUE],
            "time": {
                "temporal_role": ORDERED_AT,
                "grain": "month",
                "start": "2024-06-01",
                "end": "2024-11-01",
            },
        },
        "2024-10-15T00:00:00Z",
    ),
}


def _west_cases() -> dict[str, tuple[dict[str, Any], str | None]]:
    """The ledger's West answers, then the extra reads above."""
    west = {"roles": ["regional"], "attributes": {"region": "West"}}
    cases = {
        case_id: (spec["query"], spec.get("now"))
        for case_id, spec in load_entries()
        if spec.get("context") == west and spec["expect"] == "answer"
    }
    return {**cases, **EVERY_OCCURRENCE}


WEST_CASES = _west_cases()


def _package(root: Path, seed: str = "", edit: Callable[[Path], None] | None = None) -> Runtime:
    package = _write_variant(root, "utc_authored")
    if seed:
        path = package / "data" / "seed.sql"
        path.write_text(path.read_text(encoding="utf-8") + seed, encoding="utf-8")
    if edit is not None:
        edit(package)
    return _runtime(package)


@pytest.fixture(scope="module")
def runtimes(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Runtime]]:
    def home_stores(package: Path) -> None:
        """Customers get a home store; 101 and 103 live by Oak (East) and buy at Elm (West)."""
        path = package / "models" / "signups.yml"
        model = yaml.safe_load(path.read_text(encoding="utf-8"))
        model["model"]["entities"]["store"] = {"expr": "home_store_id"}
        path.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")

    home_seed = """
ALTER TABLE signups ADD COLUMN home_store_id VARCHAR(8);
UPDATE signups SET home_store_id = CASE WHEN customer_id IN (101, 103) THEN 'b' ELSE 'a' END;
"""
    built = {
        "shop": {},
        "visible_to_west": {"seed": VISIBLE_TO_WEST},
        "hidden_duplicate_key": {"seed": HIDDEN_DUPLICATE_KEY},
        "hidden_rows_changed": {"seed": HIDDEN_ROWS_CHANGED},
        "home_stores": {"seed": home_seed, "edit": home_stores},
    }
    out: dict[str, Runtime] = {}
    try:
        for name, options in built.items():
            out[name] = _package(tmp_path_factory.mktemp(name), **options)
            # Connect while the fixture sets up, so the connection outlives each test, in the
            # suite's session zone (GLOBAL: the engine queries on cursors).
            _rows(out[name], f"SET GLOBAL TimeZone = '{SESSION_ZONE}'")
        yield out
    finally:
        for runtime in out.values():
            runtime.close()


def _response(result: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    """Everything a caller reads in an answer, without timings, traces or package paths; rows
    in any order unless the query orders them."""
    keys = ("row_count", "truncated", "status", "warnings", "assumptions", "clock")
    keys += ("observation_scope", "output_columns", "column_types", "rendered_sql")
    rows = comparable(result, ordered=bool(query.get("order_by")))
    return {"rows": rows, **{key: result.get(key) for key in keys}}


@pytest.mark.parametrize("case_id", WEST_CASES)
def test_a_west_answer_is_the_unfiltered_answer_on_the_west_rows(runtimes, case_id):
    query, now = WEST_CASES[case_id]
    expected = runtimes["visible_to_west"].query(query, now=now)
    everyone = runtimes["shop"].query(query, now=now)
    west = runtimes["shop"].query(query, context=WEST, now=now)
    assert comparable(west) == comparable(expected), case_id
    if case_id in EVERY_OCCURRENCE:
        assert comparable(everyone) != comparable(expected), "hidden rows must change the answer"


@pytest.mark.parametrize("case_id", [case for case in WEST_CASES if case.startswith("shop/")])
def test_the_frozen_west_rows_are_the_unfiltered_answer_on_the_west_rows(runtimes, case_id):
    """The ledger's West rows, checked against the engine on the West's rows alone (this
    holds today: it needs no row filter)."""
    spec = dict(load_entries())[case_id]
    visible = runtimes["visible_to_west"]
    answer = visible.query(spec["query"], now=call_arguments(spec)["now"])
    assert comparable(answer) == comparable(encode(visible, spec)), case_id


@pytest.mark.parametrize("variant", ["hidden_duplicate_key", "hidden_rows_changed"])
@pytest.mark.parametrize("case_id", WEST_CASES)
def test_a_hidden_only_change_moves_nothing_in_a_west_answer(runtimes, variant, case_id):
    query, now = WEST_CASES[case_id]
    before = runtimes["shop"].query(query, context=WEST, now=now)
    after = runtimes[variant].query(query, context=WEST, now=now)
    assert _response(after, query) == _response(before, query), case_id


def test_a_hidden_duplicate_key_shows_its_own_region_the_orders_it_owns(runtimes):
    """Duplicated owner keys are rows like any other: the East sees Elm's orders under the
    East copy of Elm's key, and the West still sees them under Elm alone."""
    duplicate = runtimes["hidden_duplicate_key"]
    east = duplicate.query(REVENUE_BY_STORE, context=EAST)
    west = duplicate.query(REVENUE_BY_STORE, context=WEST)

    def stores(result):
        return sorted(
            (row["dimension.shop_store_id"], row[STORE_NAME], str(row["revenue"]))
            for row in result["rows"]
        )

    assert stores(east) == [("a", "Secret", "43"), ("b", "Oak", "25")]
    assert stores(west) == [("a", "Elm", "43")]


def _held(runtime: Runtime, query: dict[str, Any]) -> str:
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(query, context=WEST)
    error = raised.value
    assert error.code == "POLICY_DENIED"
    report = runtime.validate(query, context=WEST)
    assert not report["ok"] and report["errors"][0]["details"]["reason"] == error.details["reason"]
    shown = json.dumps([str(error), error.details, report["errors"]], default=str)
    assert not any(value in shown for value in ("Oak", "East")), shown
    return error.details["reason"]


@pytest.mark.parametrize(
    "query",
    [
        # How many customers do we have? Customers hold no store; a West manager is offered
        # the customers who bought in their stores instead.
        {"select": ["measure.shop.signup_count"]},
        # List the tags: tag links belong to their order, but the tags themselves to no store.
        {"group_by": ["dimension.shop_tag_tag_name"]},
    ],
    ids=["count-customers", "list-tags"],
)
def test_a_question_no_region_reaches_is_held(runtimes, query):
    assert _held(runtimes["shop"], query) == "row_filter_uncovered_root"


def test_revenue_by_a_customers_home_store_is_held(runtimes):
    """Customers 101 and 103 live by Oak and buy at Elm: grouping a West manager's orders by
    their customers' home store would put Elm's orders in a store the manager can't see."""
    query = {
        **REVENUE_BY_STORE,
        "routes": [
            {
                "from": "entity.shop_order",
                "to": "entity.shop_store",
                "via": ["relationship.orders_customer", "relationship.signups_store"],
            }
        ],
    }
    home_stores = runtimes["home_stores"]
    everyone = home_stores.query(query)
    assert {row[STORE_NAME]: str(row["revenue"]) for row in everyone["rows"]} == {
        "Elm": "26",
        "Oak": "46",
        None: "2",
    }
    assert _held(home_stores, query) == "row_filter_route_not_owned"


def _values(runtime: Runtime, dimension: str, *, live: bool, context=None) -> dict[str, Any]:
    seed = runtime.engine_seed({"policy_context": context.to_policy_context()}) if context else None
    return operations.run(
        "valid-values", runtime, dimension_id=dimension, query=seed, allow_live_query=live
    )


@pytest.mark.parametrize(
    ("dimension", "visible", "hidden"),
    [(STORE_NAME, {"Elm", "Fir"}, "Oak"), ("dimension.shop_store_region", {"West"}, "East")],
    ids=["store", "region"],
)
def test_the_picker_lists_only_values_the_caller_can_see(runtimes, dimension, visible, hidden):
    """Every store name and region is authored in the package, and an unfiltered picker lists
    them all; a West manager's picker reads the stores they can see, live, or asks for a live
    read, and never lists a hidden one."""
    shop = runtimes["shop"]
    authored = _values(shop, dimension, live=False)
    assert hidden in [row["value"] for row in authored["values"]]
    live = [row["value"] for row in _values(shop, dimension, live=True, context=WEST)["values"]]
    assert live and set(live) <= visible, live
    declared = _values(shop, dimension, live=False, context=WEST)
    assert hidden not in [row["value"] for row in declared["values"]], declared
