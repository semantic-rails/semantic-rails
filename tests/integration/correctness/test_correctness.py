"""Differential correctness: the same questions on DuckDB and Postgres, checked against SQL.

Every case pairs a semantic query with reference SQL written independently of the engine,
in SQL that DuckDB and Postgres both run. Each backend's answer must equal the reference on
that backend, and Postgres must answer exactly as DuckDB does. The package, its seed and
the package variants (clock x calendar) are described in ``conftest.py`` and
``shop/data/seed.sql``.

A known wrong answer is a strict ``xfail`` naming the defect, so its fix turns the case
into a pass (and forgets nothing: an unexpected pass fails until the mark is removed).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as clock_time
from decimal import Decimal
from typing import Any

import pytest

from semantic_rails.compiler import compile_query
from tests.semantic_rails.empty_groups_invariant import assert_settled_in_one_place

from .conftest import Backend

ROLE = "temporal_role.shop_order_ordered_at"
SIGNUP_ROLE = "temporal_role.shop_customer_signed_up_at"
STORE = "dimension.shop_order_store_id"
CHANNEL = "dimension.shop_customer_channel"  # looked up from the order's customer
REVENUE = {"measure": "measure.shop.revenue"}
ORDERS = {"measure": "measure.shop.order_count"}
AVERAGE = {"measure": "measure.shop.average_order"}
SMALLEST = {"measure": "measure.shop.smallest_order"}
LARGEST = {"measure": "measure.shop.largest_order"}
REFUNDS = {"measure": "measure.shop.refund_count"}
GOODS = {"measure": "measure.shop.goods_refunded"}
SHIPPING = {"measure": "measure.shop.shipping_refunded"}
TAX = {"measure": "measure.shop.tax_refunded"}
REFUND_TYPE = "dimension.shop_refund_refund_type"
# Plain measures by alias: the query input, and the SQL the reference computes for it. A sum
# of nothing reads 0 where the measure has amounts elsewhere (one order has a NULL amount).
PLAIN = {
    "revenue": (REVENUE, "COALESCE(SUM(o.amount), 0)"),
    "orders": (ORDERS, "COUNT(*)"),
    "average": (AVERAGE, "AVG(o.amount)"),
    "smallest": (SMALLEST, "MIN(o.amount)"),
    "largest": (LARGEST, "MAX(o.amount)"),
}

# The order clock per package variant, spelled independently of the engine's rewrite.
CLOCK = {
    "utc": "o.ordered_at",
    "ny": "((o.ordered_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York')",
    "date": "CAST(o.order_date AS TIMESTAMP)",
    "tz": "(o.ordered_at_tz AT TIME ZONE 'UTC')",
}
STEP = {"day": "1 day", "week": "7 day", "month": "1 month", "quarter": "3 month", "year": "1 year"}

# Known wrong answers.
EMPTY_WINDOW_NULL = (
    "time.fill buckets in a window with no rows read NULL until the engine checks for data"
    " outside the window (https://github.com/semantic-rails/semantic-rails/issues/201);"
    " the reference is the 0 the rule gives"
)


def BOTH(reason: str) -> dict[str, str]:  # noqa: N802 - reads as a constant at the call sites
    """A wrong answer on both backends: each misses the reference, and they still agree."""
    return {"duckdb": reason, "postgres": reason}


@dataclass(frozen=True)
class Case:
    name: str
    variant: str
    query: dict[str, Any]
    reference: str
    # Whether the rollup must (True) or must not (False) answer; None when it doesn't matter.
    routes: bool | None = None
    # Known wrong answers, by check: "duckdb", "postgres" (against the reference) or "agree".
    known: dict[str, str] = field(default_factory=dict)


# -- query builders ---------------------------------------------------------------------


def _item(expression: dict[str, Any], alias: str) -> dict[str, Any]:
    return {"expression": expression, "as": alias}


def _ask(grain: str, *select: dict[str, Any], role: str = ROLE, **extra: Any) -> dict[str, Any]:
    time = {"temporal_role": role, "grain": grain}
    for key in ("start", "end", "fill", "calendar_id"):
        if key in extra:
            time[key] = extra.pop(key)
    return {"select": list(select), "time": time, **extra}


def _prior(unit: str, value: int = 1) -> dict[str, Any]:
    return _item(
        {"kind": "prior_period", "input": REVENUE, "offset": {"unit": unit, "value": value}},
        "prior",
    )


def _rolling(unit: str, value: int) -> dict[str, Any]:
    return _item(
        {"kind": "rolling", "input": REVENUE, "window": {"unit": unit, "value": value}}, "trailing"
    )


def _distribution(function: str, alias: str, **extra: Any) -> dict[str, Any]:
    over = {"kind": "entity_value", "entity": "entity.shop_order", "input": REVENUE}
    return _item({"kind": "distribution", "function": function, "over": over, **extra}, alias)


def _predicate(entity: str, scope: str, measure: dict[str, Any], op: str, value: Any) -> dict:
    expression = {"kind": "metric_predicate", "entity": entity, "scope_mode": scope}
    expression |= {"input": measure, "op": op, "value": value}
    return {"expression": expression, "op": "=", "value": True}


LARGE_ORDER = _predicate("entity.shop_order", "contextual", REVENUE, ">", 5)
REPEAT_CUSTOMER = _predicate("entity.shop_customer", "entity_only", ORDERS, ">=", 2)
BIG_CUSTOMER_MONTH = _predicate("entity.shop_customer", "contextual", REVENUE, ">", 7)

# -- reference builders -----------------------------------------------------------------


def _by(grain: str, value: str, *, clock: str = "utc", where: str = "TRUE", store=False) -> str:
    """One row per bucket (and store) with data: ``value`` over the orders ``o``."""
    head, group = ("o.store_id, ", "1, 2") if store else ("", "1")
    return (
        f"SELECT {head}date_trunc('{grain}', {CLOCK[clock]}) AS b, {value}"
        f" FROM orders AS o WHERE {where} GROUP BY {group}"
    )


def _within(start: str, end: str, clock: str = "utc") -> str:
    return f"{CLOCK[clock]} >= TIMESTAMP '{start}' AND {CLOCK[clock]} < TIMESTAMP '{end}'"


def _span(bounds: tuple[str, ...]) -> str:
    """Series bounds: the first and last bucket with data, or ``bounds`` (start, last bucket)."""
    if bounds:
        return f"TIMESTAMP '{bounds[0]}', TIMESTAMP '{bounds[1]}'"
    return "(SELECT MIN(b) FROM m), (SELECT MAX(b) FROM m)"


def _series(grain: str, value: str, *, clock: str = "utc") -> str:
    """One row per bucket of a gapless series, plus ``value``.

    ``value`` reads ``s.b`` (the bucket) and ``m`` (revenue per bucket with data). The series
    runs from the first to the last bucket with data.
    """
    return f"""
        WITH m AS (
          SELECT date_trunc('{grain}', {CLOCK[clock]}) AS b, SUM(o.amount) AS v
          FROM orders AS o GROUP BY 1
        ),
        s AS (SELECT g.b FROM generate_series({_span(())}, INTERVAL '{STEP[grain]}') AS g(b))
        SELECT s.b, {value} FROM s
    """


def _by_store(value: str, *, bounds: tuple[str, str] = ()) -> str:
    """One row per store and month of the series (like ``_series``), plus ``value``."""
    return f"""
        WITH m AS (
          SELECT o.store_id AS k, date_trunc('month', o.ordered_at) AS b, SUM(o.amount) AS v
          FROM orders AS o GROUP BY 1, 2
        ),
        s AS (SELECT g.b FROM generate_series({_span(bounds)}, INTERVAL '1 month') AS g(b)),
        k AS (SELECT DISTINCT m.k FROM m)
        SELECT k.k, s.b, {value} FROM k CROSS JOIN s
    """


# Revenue in the bucket: 0 without orders, and 0 when its only amounts are NULL.
NOW = "(SELECT COALESCE(MAX(m.v), 0) FROM m WHERE m.b = s.b)"
# Per store (``k``, NULL included) and bucket: 0 without orders, and 0 when the only amount is NULL.
STORE_NOW = "(SELECT COALESCE(MAX(m.v), 0) FROM m WHERE m.k IS NOT DISTINCT FROM k.k AND m.b = s.b)"


def _at(offset: str) -> str:
    """Revenue ``offset`` before the bucket (like ``NOW``), and NULL before the series."""
    shifted = NOW.replace("m.b = s.b", f"m.b = s.b - INTERVAL '{offset}'")
    return (
        f"CASE WHEN s.b - INTERVAL '{offset}' < (SELECT MIN(b) FROM s) THEN NULL ELSE {shifted} END"
    )


def _trailing(span: str) -> str:
    return (
        f"(SELECT COALESCE(SUM(m.v), 0) FROM m WHERE m.b <= s.b AND m.b > s.b - INTERVAL '{span}')"
    )


def _per_order(aggregate: str, grain: str = "month") -> str:
    """``aggregate`` of the per-order revenue (one order per row) in each bucket."""
    return f"(SELECT {aggregate} FROM orders AS o WHERE date_trunc('{grain}', o.ordered_at) = s.b)"


def _fiscal(grain: str, clock: str, start: str, end: str) -> str:
    """Revenue per fiscal ``grain`` over the calendar's days in [start, end), 0 without orders."""
    return (
        f"SELECT f.{grain}_start, COALESCE(SUM(o.amount), 0) FROM dim_fiscal AS f"
        f" LEFT JOIN orders AS o ON f.date_day = CAST({CLOCK[clock]} AS DATE)"
        f" WHERE f.date_day >= DATE '{start}' AND f.date_day < DATE '{end}' GROUP BY 1"
    )


def _data_months(value: str) -> str:
    """One row per month with orders, plus ``value``, which reads ``s.b`` and ``s.v``."""
    return f"""
        WITH m AS (
          SELECT date_trunc('month', o.ordered_at) AS b, SUM(o.amount) AS v
          FROM orders AS o GROUP BY 1
        )
        SELECT s.b, {value} FROM m AS s
    """


CUMULATIVE = "SUM(s.v) OVER (ORDER BY s.b)"
QUARTER_TO_DATE = "SUM(s.v) OVER (PARTITION BY date_trunc('quarter', s.b) ORDER BY s.b)"
P80 = "percentile_cont(0.8) WITHIN GROUP (ORDER BY o.amount)"
MEDIAN = "percentile_cont(0.5) WITHIN GROUP (ORDER BY o.amount)"
PRIOR_MONTH, TRAILING_3 = _at("1 month"), _trailing("3 month")
MONTH_P80, MONTH_MEDIAN = _per_order(P80), _per_order(MEDIAN)
# The order's customer's channel: NULL when the signup has none or there is no signup.
ORDER_CHANNEL = "(SELECT s.channel FROM signups AS s WHERE s.customer_id = o.customer_id)"
REPEAT_CUSTOMER_SQL = (
    "o.customer_id IN (SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(*) >= 2)"
)
BIG_CUSTOMER_MONTH_SQL = (
    "EXISTS (SELECT 1 FROM orders AS x WHERE x.customer_id = o.customer_id"
    " AND date_trunc('month', x.ordered_at) = date_trunc('month', o.ordered_at)"
    " HAVING SUM(x.amount) > 7)"
)
FILL_WINDOW = """
    WITH m AS (
      SELECT date_trunc('month', {clock}) AS b, SUM(o.amount) AS v,
        AVG(o.amount) AS a
      FROM orders AS o GROUP BY 1
    )
    SELECT g.b, COALESCE(m.v, 0), m.a
    FROM generate_series(TIMESTAMP '2023-10-01', TIMESTAMP '2024-08-01',
      INTERVAL '1 month') AS g(b)
    LEFT JOIN m ON m.b = g.b
"""
CONVERSION = """
    WITH c AS (
      SELECT s.signed_up_at, (
        SELECT MIN(o.ordered_at) FROM orders AS o
        WHERE o.customer_id = s.customer_id AND o.ordered_at >= s.signed_up_at
      ) AS converted_at
      FROM signups AS s
    )
    SELECT date_trunc('{grain}', signed_up_at) AS b,
      1.0 * SUM(CASE WHEN converted_at < signed_up_at + INTERVAL '7 day' THEN 1 ELSE 0 END)
        / COUNT(*) AS rate
    FROM c GROUP BY 1
"""


# -- cases ------------------------------------------------------------------------------


def _plain(
    name: str,
    variant: str,
    grain: str,
    aliases: str = "revenue",
    *,
    sql_where: str = "TRUE",
    store: bool = False,
    routes: bool | None = None,
    known: dict | None = None,
    **extra: Any,
) -> Case:
    """Plain measures (``aliases``) per bucket and store: the query and its GROUP BY reference."""
    if store:
        extra["group_by"] = [STORE]
    names = aliases.split()
    query = _ask(grain, *(_item(PLAIN[alias][0], alias) for alias in names), **extra)
    value = ", ".join(PLAIN[alias][1] for alias in names)
    clock = variant.split("_")[0]
    reference = _by(grain, value, clock=clock, where=sql_where, store=store)
    return Case(name, variant, query, reference, routes, known or {})


def _dense(
    name: str,
    variant: str,
    grain: str,
    select: list,
    value: str,
    known: dict | None = None,
    **extra: Any,
) -> Case:
    """``select`` over the gapless series in the variant's clock, against ``_series``."""
    reference = _series(grain, value, clock=variant.split("_")[0])
    return Case(name, variant, _ask(grain, *select, **extra), reference, known=known or {})


def _empty_group_cases(revenue: dict[str, Any], orders: dict[str, Any]) -> Iterator[Case]:
    """A group with no rows reads 0 where its measure has data in scope, NULL where it has none.

    Refunds are a second fact: only stores 2, 4 and 6 have any, and their amounts are pivoted by
    type (each type leaves the other amount columns NULL; tax is never filled).
    """
    by_store = {"group_by": [STORE]}
    by_type = {"group_by": [REFUND_TYPE]}
    plus = {"kind": "arithmetic", "op": "add"}
    goods_and_shipping = {**plus, "left": GOODS, "right": SHIPPING}
    goods_and_tax = {**plus, "left": GOODS, "right": TAX}
    refunds = _item(REFUNDS, "refunds")
    # A count beside another fact: the store with no refunds is a group the refund leaf lacks.
    yield Case(
        "count_beside_a_second_fact",
        "utc_authored",
        {"select": [revenue, refunds], **by_store},
        """
        WITH o AS (SELECT store_id, SUM(amount) AS v FROM orders GROUP BY 1),
        r AS (
          SELECT o.store_id, COUNT(*) AS n
          FROM refunds AS f JOIN orders AS o ON o.order_id = f.order_id GROUP BY 1
        )
        SELECT o.store_id, o.v, COALESCE(r.n, 0)
        FROM o LEFT JOIN r ON r.store_id IS NOT DISTINCT FROM o.store_id
        """,
    )
    # Amounts pivoted by type, added: the column a type leaves NULL is 0, not a NULL sum.
    yield Case(
        "pivot_amounts_added_by_type",
        "utc_authored",
        {
            "select": [
                _item(goods_and_shipping, "total"),
                _item(GOODS, "goods"),
                _item(SHIPPING, "shipping"),
            ],
            **by_type,
        },
        """
        SELECT refund_type, COALESCE(SUM(goods_amount), 0) + COALESCE(SUM(shipping_amount), 0),
          COALESCE(SUM(goods_amount), 0), COALESCE(SUM(shipping_amount), 0)
        FROM refunds GROUP BY 1
        """,
    )
    # A measure with no amount anywhere reads NULL in every group, and so does its sum with
    # another: no data is not zero.
    yield Case(
        "unobserved_measure_reads_null",
        "utc_authored",
        {"select": [_item(TAX, "tax"), _item(goods_and_tax, "goods_and_tax")], **by_type},
        "SELECT refund_type, SUM(tax_amount), SUM(goods_amount) + SUM(tax_amount) FROM refunds"
        " GROUP BY 1",
    )
    # A filter that matches nothing: a sum and a count of nothing read NULL, not 0.
    absent = [{"field": STORE, "op": "=", "value": "zzz"}]
    yield Case(
        "absent_filter_reads_null",
        "utc_authored",
        {"select": [revenue, orders], "where": absent},
        "SELECT SUM(amount), NULLIF(COUNT(*), 0) FROM orders WHERE store_id = 'zzz'",
    )
    # ...also beside an input that has data: only the input that has none reads NULL.
    absent_only = _item(
        {"kind": "aggregate", "measure": REVENUE["measure"], "filter": {"all": absent}}, "absent"
    )
    yield Case(
        "absent_filtered_input_beside_an_observed_one",
        "utc_authored",
        _ask("quarter", revenue, absent_only),
        _by(
            "quarter",
            "COALESCE(SUM(o.amount), 0), SUM(CASE WHEN o.store_id = 'zzz' THEN o.amount END)",
        ),
    )
    # A ratio over an empty numerator is 0: store a's May amounts are all NULL, its one order counts.
    average_value = _item({"metric": "metric.shop.average_order_value"}, "aov")
    yield Case(
        "ratio_over_an_empty_numerator",
        "utc_authored",
        _ask("month", average_value, **by_store),
        _by("month", "1.0 * COALESCE(SUM(o.amount), 0) / COUNT(*)", store=True),
    )


def _absent_entity_cases() -> Iterator[Case]:
    """ "Customers with no large orders": an entity reads like any other, listed or not.

    Signups are the customers. 107 never ordered, 105's only order has no amount, and only
    customers 101 and 103 have an order of 10 or more. A count of the orders at or above a floor
    is 0, not NULL, for a customer whose orders all fall short, and no order reaches 1000.
    """
    signups = _item({"measure": "measure.shop.signup_count"}, "signups")
    floor_10 = {"measure": "measure.shop.large_order_count"}
    floor_1000 = {"measure": "measure.shop.huge_order_count"}

    def count_if(floor: int) -> dict[str, Any]:
        condition = {
            "kind": "comparison",
            "op": ">=",
            "left": {"kind": "column", "column": "amount", "entity": "entity.shop_order"},
            "right": {"kind": "literal", "value": floor},
        }
        return {"kind": "aggregate_if", "aggregation": "count", "condition": condition}

    def reference(floor: int, count: str) -> str:
        # The rule: with some order at or above the floor, a customer with none reads 0 (the
        # ones with no orders at all too); with none anywhere, every customer reads NULL.
        return f"""
            SELECT {count} FROM signups AS s
            WHERE EXISTS (SELECT 1 FROM orders WHERE amount >= {floor})
              AND (SELECT COUNT(*) FROM orders AS o
                   WHERE o.customer_id = s.customer_id AND o.amount >= {floor}) = 0
        """

    for form, floor_10_input, floor_1000_input in (
        ("case_count", floor_10, floor_1000),
        ("aggregate_if", count_if(10), count_if(1000)),
    ):
        for name, predicate_input, floor, count in (
            ("one_match_keeps_every_other_customer", floor_10_input, 10, "COUNT(*)"),
            ("no_match_keeps_no_customer", floor_1000_input, 1000, "NULLIF(COUNT(*), 0)"),
        ):
            predicate = _predicate("entity.shop_customer", "entity_only", predicate_input, "=", 0)
            yield Case(
                f"customers_with_no_large_orders-{form}-{name}",
                "utc_authored",
                {"select": [signups], "metric_filters": [predicate]},
                reference(floor, count),
            )


def _cases() -> Iterator[Case]:
    revenue, average = _item(REVENUE, "revenue"), _item(AVERAGE, "average")
    orders = _item(ORDERS, "orders")
    prior_month, trailing_3 = _prior("month"), _rolling("month", 3)
    median, p80 = _distribution("median", "median"), _distribution("percentile", "p80", p=0.8)
    cumulative = _item({"kind": "cumulative", "input": REVENUE}, "cumulative")
    qtd = _item({"kind": "period_to_date", "input": REVENUE, "period": "quarter"}, "qtd")

    # Grains and calendars: every clock, with and without the authored calendars. Month and
    # coarser route to the rollup, which was built in UTC.
    for clock in CLOCK:
        for calendar in ("authored", "implicit"):
            variant = f"{clock}_{calendar}"
            for grain in ("day", "week", "month", "quarter", "year"):
                yield _plain(f"{variant}-revenue_by_{grain}", variant, grain)
    # Fiscal quarters: order 11 crosses into the previous one in New York, and the window
    # starts and ends with an empty quarter there (the first one in every clock).
    for clock, grain, start, end in [
        *((clock, "quarter", "2023-08-01", "2024-11-01") for clock in CLOCK),
        ("utc", "year", "2023-02-01", "2025-02-01"),
    ]:
        query = _ask(grain, revenue, calendar_id="fiscal", start=start, end=end, fill=True)
        reference = _fiscal(grain, clock, start, end)
        yield Case(f"{clock}-fiscal_{grain}", f"{clock}_authored", query, reference)

    # Nulls, groups, filters and bounds; whole-month bounds may use the rollup.
    # Store a has only a NULL amount in May: its sum reads 0 (the measure has amounts elsewhere),
    # while an average, minimum or maximum of nothing stays NULL.
    yield _plain(
        "null_store_group",
        "utc_authored",
        "month",
        "revenue orders average smallest largest",
        store=True,
    )
    yield _plain(
        "store_is_null",
        "utc_implicit",
        "month",
        where=[{"field": STORE, "op": "IS NULL"}],
        sql_where="o.store_id IS NULL",
    )
    yield from _empty_group_cases(revenue, orders)
    yield from _absent_entity_cases()
    for name, variant, grain, start, end, routes in (
        ("utc-march_bounds_by_day", "utc_implicit", "day", "2024-03-01", "2024-04-01", None),
        ("ny-march_bounds_by_day", "ny_implicit", "day", "2024-03-01", "2024-04-01", None),
        ("rollup_month_bounds", "utc_authored", "month", "2024-01-01", "2024-07-01", True),
        (
            "rollup_skipped_for_mid_month_bounds",
            "utc_authored",
            "month",
            "2024-01-15",
            "2024-06-15",
            False,
        ),
    ):
        where = _within(start, end, variant.split("_")[0])
        yield _plain(name, variant, grain, start=start, end=end, sql_where=where, routes=routes)
    yield Case(
        "empty_window",
        "utc_authored",
        _ask("month", revenue, start="2024-02-01", end="2024-03-01"),
        "SELECT 1 WHERE FALSE",
    )
    for name, grain, aliases, predicate, where in (
        ("large_orders", "month", "revenue", LARGE_ORDER, "o.amount > 5"),
        ("repeat_customers", "quarter", "revenue orders", REPEAT_CUSTOMER, REPEAT_CUSTOMER_SQL),
        ("big_customer_months", "month", "revenue", BIG_CUSTOMER_MONTH, BIG_CUSTOMER_MONTH_SQL),
    ):
        yield _plain(
            name, "utc_authored", grain, aliases, sql_where=where, metric_filters=[predicate]
        )
    # A lookup keeps orders it finds no customer for (order 11): they group under NULL with
    # signup 105's, and a filter on the looked-up column drops them unless it asks for NULL.
    orders = _item(ORDERS, "orders")
    lookup = f"SELECT {ORDER_CHANNEL}, SUM(o.amount), COUNT(*) FROM orders AS o"
    yield Case(
        "lookup_by_channel",
        "utc_authored",
        {"select": [revenue, orders], "group_by": [CHANNEL]},
        f"{lookup} GROUP BY 1",
    )
    for name, op, value, where in (
        ("lookup_channel_is_null", "IS NULL", None, "IS NULL"),
        ("lookup_channel_not_web", "!=", "web", "<> 'web'"),
    ):
        filter_ = {"field": CHANNEL, "op": op, **({"value": value} if value else {})}
        yield Case(
            name,
            "utc_authored",
            {"select": [revenue, orders], "group_by": [CHANNEL], "where": [filter_]},
            f"{lookup} WHERE {ORDER_CHANNEL} {where} GROUP BY 1",
        )
    store_a = {"all": [{"field": STORE, "op": "=", "value": "a"}]}
    only_a = _item({"kind": "aggregate", "measure": REVENUE["measure"], "filter": store_a}, "a")
    yield Case(
        "filtered_input",
        "utc_authored",
        _ask("quarter", revenue, only_a),
        _by(
            "quarter",
            "SUM(o.amount), COALESCE(SUM(CASE WHEN o.store_id = 'a' THEN o.amount END), 0)",
        ),
    )

    # Rollup routing: the monthly rollup (DATE keys) must answer exactly as the base table.
    for variant in ("utc_authored", "utc_implicit", "date_authored", "tz_authored"):
        for grain in ("month", "quarter", "year"):
            name = f"{variant}-rollup_by_{grain}_and_store"
            yield _plain(name, variant, grain, "revenue orders", store=True, routes=True)
    yield _plain("rollup_beside_base_average", "utc_authored", "month", "revenue average")
    yield _plain("rollup_skipped_for_a_zone", "ny_authored", "month", routes=False)

    # Dense fill: zero for additive measures, NULL for the rest, over the window.
    for variant in ("utc_authored", "utc_implicit", "ny_implicit", "date_authored", "tz_implicit"):
        clock = variant.split("_")[0]
        query = _ask("month", revenue, average, start="2023-10-01", end="2024-09-01", fill=True)
        reference = FILL_WINDOW.format(clock=CLOCK[clock])
        yield Case(f"{variant}-fill_window", variant, query, reference)
    yield Case(
        "fill_empty_window",
        "utc_authored",
        _ask("month", revenue, start="2024-02-01", end="2024-03-01", fill=True),
        "SELECT TIMESTAMP '2024-02-01', 0",
        known=BOTH(EMPTY_WINDOW_NULL),
    )

    # Windows over the series: rolling, prior_period, cumulative, period_to_date. The week
    # of 2024-05-06 and that day hold only order 7 (NULL amount).
    for variant in (
        "utc_authored",
        "utc_implicit",
        "ny_authored",
        "ny_implicit",
        "date_authored",
        "tz_authored",
    ):
        yield _dense(f"{variant}-trailing_3_months", variant, "month", [trailing_3], TRAILING_3)
        yield _dense(
            f"{variant}-revenue_and_prior_month",
            variant,
            "month",
            [revenue, prior_month],
            f"{NOW}, {PRIOR_MONTH}",
        )
    for grain in ("day", "week", "quarter", "year"):
        yield _dense(f"prior_{grain}", "utc_authored", grain, [_prior(grain)], _at(STEP[grain]))
    yield _dense("prior_year_by_month", "utc_implicit", "month", [_prior("year")], _at("1 year"))
    yield _dense("trailing_7_days", "utc_implicit", "day", [_rolling("day", 7)], _trailing("7 day"))
    yield _dense(
        "prior_and_trailing_two_quarters",
        "utc_implicit",
        "quarter",
        [_prior("quarter", 2), _rolling("quarter", 2)],
        f"{_at('6 month')}, {_trailing('6 month')}",
    )
    yield _dense(
        "ny_prior_week",
        "ny_implicit",
        "week",
        [revenue, _prior("week")],
        f"{NOW}, {_at('7 day')}",
    )
    yield _dense(
        "filled_cumulative_and_quarter_to_date",
        "utc_authored",
        "month",
        [cumulative, qtd],
        "(SELECT SUM(m.v) FROM m WHERE m.b <= s.b),"
        " (SELECT COALESCE(SUM(m.v), 0) FROM m WHERE m.b <= s.b"
        " AND date_trunc('quarter', m.b) = date_trunc('quarter', s.b))",
        fill=True,
    )

    # Distributions of per-order revenue, alone and beside window siblings.
    for function in ("median", "avg", "min", "max", "sum"):
        reference = MEDIAN if function == "median" else f"{function.upper()}(o.amount)"
        query = _ask("month", _distribution(function, function))
        yield Case(f"distribution_{function}", "utc_authored", query, _by("month", reference))
    query = _ask("quarter", p80, group_by=[STORE])
    yield Case("distribution_p80_by_store", "utc_authored", query, _by("quarter", P80, store=True))
    for name, select, value in (
        ("distribution_and_rolling", [p80, trailing_3], f"{MONTH_P80}, {TRAILING_3}"),
        ("distribution_and_prior", [median, prior_month], f"{MONTH_MEDIAN}, {PRIOR_MONTH}"),
        ("rolling_and_distribution", [trailing_3, p80], f"{TRAILING_3}, {MONTH_P80}"),
    ):
        yield _dense(name, "utc_authored", "month", select, value)
    yield _dense(
        "distribution_revenue_and_prior",
        "utc_authored",
        "month",
        [revenue, median, prior_month],
        f"{NOW}, {MONTH_MEDIAN}, {PRIOR_MONTH}",
    )
    for name, select, value in (
        ("cumulative_and_quarter_to_date", [cumulative, qtd], f"{CUMULATIVE}, {QUARTER_TO_DATE}"),
        ("distribution_and_cumulative", [median, cumulative], f"{MONTH_MEDIAN}, {CUMULATIVE}"),
        ("distribution_and_quarter_to_date", [median, qtd], f"{MONTH_MEDIAN}, {QUARTER_TO_DATE}"),
    ):
        yield Case(name, "utc_authored", _ask("month", *select), _data_months(value))

    # Dense per-store series: every store (NULL included) in every month.
    store_prior = STORE_NOW.replace("m.b = s.b", "m.b = s.b - INTERVAL '1 month'")
    yield Case(
        "prior_month_by_store",
        "utc_authored",
        _ask("month", revenue, prior_month, group_by=[STORE]),
        _by_store(
            f"{STORE_NOW}, CASE WHEN s.b = (SELECT MIN(b) FROM s) THEN NULL ELSE {store_prior} END"
        ),
    )
    yield Case(
        "trailing_3_months_by_store",
        "utc_implicit",
        _ask("month", trailing_3, group_by=[STORE]),
        _by_store(
            "(SELECT COALESCE(SUM(m.v), 0) FROM m WHERE m.k IS NOT DISTINCT FROM k.k"
            " AND m.b <= s.b AND m.b > s.b - INTERVAL '3 month')"
        ),
    )
    yield Case(
        "fill_by_store",
        "utc_authored",
        _ask("month", revenue, group_by=[STORE], start="2023-11-01", end="2024-08-01", fill=True),
        _by_store(STORE_NOW, bounds=("2023-11-01", "2024-07-01")),
    )

    # Conversion within 7 days of signup (half-open), by the signup's bucket. A ratio and a
    # conversion rate stay NULL in a month without a denominator.
    rate = _item({"metric": "metric.shop.signup_to_order_7d"}, "rate")
    for grain in ("month", "quarter"):
        query = _ask(grain, rate, role=SIGNUP_ROLE)
        yield Case(f"conversion_by_{grain}", "utc_authored", query, CONVERSION.format(grain=grain))
    yield _dense(
        "filled_ratio",
        "utc_authored",
        "month",
        [_item({"metric": "metric.shop.average_order_value"}, "aov")],
        "(SELECT 1.0 * SUM(o.amount) / COUNT(*) FROM orders AS o"
        " WHERE date_trunc('month', o.ordered_at) = s.b)",
        fill=True,
    )
    yield Case(
        "filled_conversion",
        "utc_authored",
        _ask("month", rate, role=SIGNUP_ROLE, fill=True),
        f"""
        WITH r AS ({CONVERSION.format(grain="month")})
        SELECT g.b, r.rate FROM generate_series(TIMESTAMP '2023-10-01',
          TIMESTAMP '2024-06-01', INTERVAL '1 month') AS g(b)
        LEFT JOIN r ON r.b = g.b
        """,
    )


CASES = list(_cases())
BACKENDS = ("duckdb", "postgres")


def _params(check: str, backends: tuple[str, ...]) -> list[Any]:
    params = []
    for case in CASES:
        for backend in backends:
            reason = case.known.get(check if check == "agree" else backend)
            # A known wrong answer fails its assertion; any other error still fails the test.
            xfail = pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)
            marks = [xfail] if reason else []
            ident = case.name if check == "agree" else f"{backend}-{case.name}"
            params.append(pytest.param(backend, case, id=ident, marks=marks))
    return params


# -- comparison -------------------------------------------------------------------------


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.date() if value.time() == clock_time(0) else value
    if isinstance(value, Decimal):
        return float(value)
    return value


def _normal(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    normal = [tuple(_value(value) for value in row) for row in rows]
    return sorted(normal, key=lambda row: [(value is None, str(value)) for value in row])


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        if left is None or right is None:
            return left is right
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def _assert_rows(expected: list[tuple[Any, ...]], actual: list[tuple[Any, ...]], label: str):
    expected, actual = _normal(expected), _normal(actual)
    same = len(expected) == len(actual) and all(
        len(e) == len(a) and all(_same(x, y) for x, y in zip(e, a, strict=True))
        for e, a in zip(expected, actual, strict=True)
    )
    assert same, (
        f"{label}\nexpected ({len(expected)} rows): {expected}\nactual ({len(actual)} rows): {actual}"
    )


def _answer(backend: Backend, case: Case) -> list[tuple[Any, ...]]:
    result = backend.runtimes[case.variant].query({"version": 1, **case.query})
    if case.routes is not None:
        routed = "orders_monthly" in str(result.get("rendered_sql") or "")
        assert routed is case.routes, f"{case.name}: rollup routing is {routed} on {backend.name}"

    def typed(value: Any, kind: str) -> Any:
        if value is None:
            return None
        if kind == "decimal":
            return Decimal(value)
        if kind == "timestamp":
            return datetime.fromisoformat(value)
        if kind == "date":
            return date.fromisoformat(value)
        return value

    return [
        tuple(typed(value, result["column_types"][key]["type"]) for key, value in row.items())
        for row in result["rows"]
    ]


def _backend(request: pytest.FixtureRequest, name: str) -> Backend:
    """Resolved per test, so a missing Postgres skips only the Postgres checks."""
    return request.getfixturevalue(f"{name}_backend")


@pytest.mark.parametrize(("backend_name", "case"), _params("reference", BACKENDS))
def test_answer_matches_reference(
    request: pytest.FixtureRequest, backend_name: str, case: Case
) -> None:
    backend = _backend(request, backend_name)
    _assert_rows(
        backend.reference(case.reference),
        _answer(backend, case),
        f"{backend_name}/{case.name}: engine answer differs from the reference SQL",
    )


@pytest.mark.parametrize(("backend_name", "case"), _params("agree", ("postgres",)))
def test_postgres_answers_as_duckdb(
    request: pytest.FixtureRequest, backend_name: str, case: Case
) -> None:
    postgres, duckdb = _backend(request, "postgres"), _backend(request, "duckdb")
    _assert_rows(
        duckdb.reference(case.reference),
        postgres.reference(case.reference),
        f"{case.name}: the reference SQL is not portable",
    )
    _assert_rows(
        _answer(duckdb, case),
        _answer(postgres, case),
        f"{case.name}: Postgres answers differently from DuckDB",
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_every_case_settles_its_empty_groups_in_the_guard(
    request: pytest.FixtureRequest, case: Case
) -> None:
    """The answers above are right because one CTE settles empty groups: hold every query to it."""
    runtime = _backend(request, "duckdb").runtimes[case.variant]
    compiled = compile_query(runtime.config, runtime.registry, {"version": 1, **case.query})
    assert_settled_in_one_place(compiled, runtime.config)
