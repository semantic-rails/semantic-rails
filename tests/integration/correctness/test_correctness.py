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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as clock_time
from decimal import Decimal
from typing import Any

import pytest

from .conftest import Backend

ROLE = "temporal_role.shop_order_ordered_at"
SIGNUP_ROLE = "temporal_role.shop_customer_signed_up_at"
STORE = "dimension.shop_order_store_id"
REVENUE = {"measure": "measure.shop.revenue"}
ORDERS = {"measure": "measure.shop.order_count"}
AVERAGE = {"measure": "measure.shop.average_order"}

# The order clock per package variant, spelled independently of the engine's rewrite.
CLOCK = {
    "utc": "o.ordered_at",
    "ny": "((o.ordered_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York')",
    "date": "CAST(o.order_date AS TIMESTAMP)",
    "tz": "(o.ordered_at_tz AT TIME ZONE 'UTC')",
}
STEP = {"day": "1 day", "week": "7 day", "month": "1 month", "quarter": "3 month", "year": "1 year"}
DUPLICATE_PERIODS = (
    "duplicate periods: a distribution beside a rolling or prior_period sibling returns every"
    " period twice on Postgres (the DATE calendar key and the TIMESTAMP bucket miss each other"
    " in the text-cast branch combine)"
)
ZONE_AWARE = (
    "zone-aware column: a TIMESTAMP WITH TIME ZONE column is bucketed in the session's time"
    " zone instead of the role's (UTC), so the answer follows the server's setting"
)
NULL_SUM_TO_ZERO = (
    "NULL sum filled as zero: once the series is dense (fill, or a window beside it), a bucket"
    " whose only amounts are NULL reads 0, where the same question unfilled reads NULL"
)


def BOTH(reason: str) -> dict[str, str]:  # noqa: N802 - reads as a constant at the call sites
    """A wrong answer on both backends: each misses the reference, and they still agree."""
    return {"duckdb": reason, "postgres": reason}


GAP_NULL = (
    "gap reads NULL: beside a distribution, revenue in a month without orders reads NULL"
    " instead of 0 (the same query without the distribution reads 0)"
)


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
    return {"kind": "prior_period", "input": REVENUE, "offset": {"unit": unit, "value": value}}


def _rolling(unit: str, value: int) -> dict[str, Any]:
    return {"kind": "rolling", "input": REVENUE, "window": {"unit": unit, "value": value}}


def _distribution(function: str, **extra: Any) -> dict[str, Any]:
    over = {"kind": "entity_value", "entity": "entity.shop_order", "input": REVENUE}
    return {"kind": "distribution", "function": function, "over": over, **extra}


def _predicate(entity: str, scope: str, measure: dict[str, Any], op: str, value: Any) -> dict:
    expression = {
        "kind": "metric_predicate",
        "entity": entity,
        "scope_mode": scope,
        "input": measure,
        "op": op,
        "value": value,
    }
    return {"expression": expression, "op": "=", "value": True}


# -- reference builders -----------------------------------------------------------------


def _by(grain: str, value: str, *, clock: str = "utc", where: str = "TRUE", keys: str = "") -> str:
    """One row per group key and bucket with data: ``value`` over the orders ``o``."""
    group = ", ".join(str(n) for n in range(1, keys.count(",") + (2 if keys else 1) + 1))
    head = f"{keys}, " if keys else ""
    return (
        f"SELECT {head}date_trunc('{grain}', {CLOCK[clock]}) AS b, {value}"
        f" FROM orders AS o WHERE {where} GROUP BY {group}"
    )


def _series(grain: str, value: str, *, clock: str = "utc", bounds: tuple[str, str] = ()) -> str:
    """One row per bucket of a gapless series, plus ``value``.

    ``value`` reads ``s.b`` (the bucket) and ``m`` (revenue per bucket with data). The series
    runs from the first to the last bucket with data, or over ``bounds`` (start, last bucket).
    """
    first, last = (
        (f"TIMESTAMP '{bounds[0]}'", f"TIMESTAMP '{bounds[1]}'")
        if bounds
        else ("(SELECT MIN(b) FROM m)", "(SELECT MAX(b) FROM m)")
    )
    return f"""
        WITH m AS (
          SELECT date_trunc('{grain}', {CLOCK[clock]}) AS b, SUM(o.amount) AS v
          FROM orders AS o GROUP BY 1
        ),
        s AS (SELECT g.b FROM generate_series({first}, {last}, INTERVAL '{STEP[grain]}') AS g(b))
        SELECT s.b, {value} FROM s
    """


# Revenue in the bucket: 0 without orders, NULL (as in a sparse answer) when its only amounts are.
NOW = "(SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE MAX(m.v) END FROM m WHERE m.b = s.b)"
# Per store (``k``, NULL included) and bucket: 0 without orders, NULL when the only amount is.
STORE_NOW = (
    "(SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE MAX(m.v) END FROM m"
    " WHERE m.k IS NOT DISTINCT FROM k.k AND m.b = s.b)"
)


def _by_store(value: str, *, bounds: tuple[str, str] = ()) -> str:
    """One row per store and month of the series (like ``_series``), plus ``value``."""
    first, last = (
        (f"TIMESTAMP '{bounds[0]}'", f"TIMESTAMP '{bounds[1]}'")
        if bounds
        else ("(SELECT MIN(b) FROM m)", "(SELECT MAX(b) FROM m)")
    )
    return f"""
        WITH m AS (
          SELECT o.store_id AS k, date_trunc('month', o.ordered_at) AS b, SUM(o.amount) AS v
          FROM orders AS o GROUP BY 1, 2
        ),
        s AS (SELECT g.b FROM generate_series({first}, {last}, INTERVAL '1 month') AS g(b)),
        k AS (SELECT DISTINCT m.k FROM m)
        SELECT k.k, s.b, {value} FROM k CROSS JOIN s
    """


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


def _fiscal(grain: str, value: str, *, clock: str = "utc") -> str:
    return (
        f"SELECT f.{grain}_start, {value} FROM orders AS o"
        f" JOIN dim_fiscal AS f ON f.date_day = CAST({CLOCK[clock]} AS DATE) GROUP BY 1"
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
LARGE_ORDER = _predicate("entity.shop_order", "contextual", REVENUE, ">", 5)
REPEAT_CUSTOMER = _predicate("entity.shop_customer", "entity_only", ORDERS, ">=", 2)
REPEAT_CUSTOMERS = "SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(*) >= 2"
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


def _cases() -> list[Case]:
    cases: list[Case] = []
    add = cases.append
    revenue = _item(REVENUE, "revenue")

    # Grains and calendars: every clock, with and without the authored calendars.
    for clock in CLOCK:
        for calendar in ("authored", "implicit"):
            variant = f"{clock}_{calendar}"
            for grain in ("day", "week", "month", "quarter", "year"):
                add(
                    Case(
                        f"{variant}-revenue_by_{grain}",
                        variant,
                        _ask(grain, revenue),
                        _by(grain, "SUM(o.amount)", clock=clock),
                        # Month and coarser route to the rollup, which was built in UTC.
                        known=BOTH(ZONE_AWARE)
                        if clock == "tz" and grain in ("day", "week")
                        else {},
                    )
                )
        add(
            Case(
                f"{clock}-fiscal_quarter",
                f"{clock}_authored",
                _ask("quarter", revenue, calendar_id="fiscal", fill=True),
                _fiscal("quarter", "SUM(o.amount)", clock=clock),
            )
        )
    add(
        Case(
            "utc-fiscal_year",
            "utc_authored",
            _ask("year", revenue, calendar_id="fiscal", fill=True),
            _fiscal("year", "SUM(o.amount)"),
        )
    )

    # Nulls, groups and bounds.
    three = [revenue, _item(ORDERS, "orders"), _item(AVERAGE, "average")]
    add(
        Case(
            "null_store_group",
            "utc_authored",
            _ask("month", *three, group_by=[STORE]),
            _by("month", "SUM(o.amount), COUNT(*), AVG(o.amount)", keys="o.store_id"),
        )
    )
    add(
        Case(
            "store_is_null",
            "utc_implicit",
            _ask("month", revenue, where=[{"field": STORE, "op": "IS NULL"}]),
            _by("month", "SUM(o.amount)", where="o.store_id IS NULL"),
        )
    )
    for clock in ("utc", "ny"):
        local = CLOCK[clock]
        add(
            Case(
                f"{clock}-march_bounds_by_day",
                f"{clock}_implicit",
                _ask("day", revenue, start="2024-03-01", end="2024-04-01"),
                _by(
                    "day",
                    "SUM(o.amount)",
                    clock=clock,
                    where=f"{local} >= TIMESTAMP '2024-03-01' AND {local} < TIMESTAMP '2024-04-01'",
                ),
            )
        )
    add(
        Case(
            "empty_window",
            "utc_authored",
            _ask("month", revenue, start="2024-02-01", end="2024-03-01"),
            "SELECT 1 WHERE FALSE",
        )
    )

    # Dense fill: zero for additive measures, NULL for the rest, over the window.
    for variant in ("utc_authored", "utc_implicit", "ny_implicit", "date_authored", "tz_implicit"):
        clock = variant.split("_")[0]
        add(
            Case(
                f"{variant}-fill_window",
                variant,
                _ask(
                    "month",
                    revenue,
                    _item(AVERAGE, "average"),
                    start="2023-10-01",
                    end="2024-09-01",
                    fill=True,
                ),
                f"""
                WITH m AS (
                  SELECT date_trunc('month', {CLOCK[clock]}) AS b, SUM(o.amount) AS v,
                    AVG(o.amount) AS a
                  FROM orders AS o GROUP BY 1
                )
                SELECT g.b, COALESCE(m.v, 0), m.a
                FROM generate_series(TIMESTAMP '2023-10-01', TIMESTAMP '2024-08-01',
                  INTERVAL '1 month') AS g(b)
                LEFT JOIN m ON m.b = g.b
                """,
                known=BOTH(ZONE_AWARE) if clock == "tz" else {},
            )
        )
    add(
        Case(
            "fill_empty_window",
            "utc_authored",
            _ask("month", revenue, start="2024-02-01", end="2024-03-01", fill=True),
            "SELECT TIMESTAMP '2024-02-01', 0",
        )
    )

    # Windows over the series: rolling, prior_period, cumulative, period_to_date.
    for variant in (
        "utc_authored",
        "utc_implicit",
        "ny_authored",
        "ny_implicit",
        "date_authored",
        "tz_authored",
    ):
        clock = variant.split("_")[0]
        add(
            Case(
                f"{variant}-trailing_3_months",
                variant,
                _ask("month", _item(_rolling("month", 3), "trailing")),
                _series("month", _trailing("3 month"), clock=clock),
            )
        )
        add(
            Case(
                f"{variant}-revenue_and_prior_month",
                variant,
                _ask("month", revenue, _item(_prior("month"), "prior")),
                _series("month", f"{NOW}, {_at('1 month')}", clock=clock),
            )
        )
    for grain in ("day", "week", "quarter", "year"):
        add(
            Case(
                f"prior_{grain}",
                "utc_authored",
                _ask(grain, _item(_prior(grain), "prior")),
                _series(grain, _at(STEP[grain])),
                # The week of 2024-05-06 and that day hold only order 7 (NULL amount).
                known=BOTH(NULL_SUM_TO_ZERO) if grain in ("day", "week") else {},
            )
        )
    add(
        Case(
            "prior_year_by_month",
            "utc_implicit",
            _ask("month", _item(_prior("year"), "prior")),
            _series("month", _at("1 year")),
        )
    )
    add(
        Case(
            "trailing_7_days",
            "utc_implicit",
            _ask("day", _item(_rolling("day", 7), "trailing")),
            _series("day", _trailing("7 day")),
        )
    )
    add(
        Case(
            "cumulative_and_quarter_to_date",
            "utc_authored",
            _ask(
                "month",
                _item({"kind": "cumulative", "input": REVENUE}, "cumulative"),
                _item({"kind": "period_to_date", "input": REVENUE, "period": "quarter"}, "qtd"),
            ),
            _data_months(f"{CUMULATIVE}, {QUARTER_TO_DATE}"),
        )
    )

    # Distributions of per-order revenue, alone and beside window siblings.
    for function, reference in (
        ("median", MEDIAN),
        ("avg", "AVG(o.amount)"),
        ("min", "MIN(o.amount)"),
        ("max", "MAX(o.amount)"),
        ("sum", "SUM(o.amount)"),
    ):
        add(
            Case(
                f"distribution_{function}",
                "utc_authored",
                _ask("month", _item(_distribution(function), function)),
                _by("month", reference),
            )
        )
    add(
        Case(
            "distribution_p80_by_store",
            "utc_authored",
            _ask("quarter", _item(_distribution("percentile", p=0.8), "p80"), group_by=[STORE]),
            _by("quarter", P80, keys="o.store_id"),
        )
    )
    p80 = _item(_distribution("percentile", p=0.8), "p80")
    median_in = _per_order(MEDIAN)
    for name, select, value in (
        (
            "distribution_and_rolling",
            [p80, _item(_rolling("month", 3), "trailing")],
            f"{_per_order(P80)}, {_trailing('3 month')}",
        ),
        (
            "distribution_and_prior",
            [_item(_distribution("median"), "median"), _item(_prior("month"), "prior")],
            f"{median_in}, {_at('1 month')}",
        ),
        (
            "rolling_and_distribution",
            [_item(_rolling("month", 3), "trailing"), p80],
            f"{_trailing('3 month')}, {_per_order(P80)}",
        ),
    ):
        add(
            Case(
                name,
                "utc_authored",
                _ask("month", *select),
                _series("month", value),
                known={"postgres": DUPLICATE_PERIODS, "agree": DUPLICATE_PERIODS},
            )
        )
    add(
        Case(
            "distribution_and_cumulative",
            "utc_authored",
            _ask(
                "month",
                _item(_distribution("median"), "median"),
                _item({"kind": "cumulative", "input": REVENUE}, "cumulative"),
            ),
            _data_months(f"{_per_order(MEDIAN)}, {CUMULATIVE}"),
        )
    )
    add(
        Case(
            "distribution_and_quarter_to_date",
            "utc_authored",
            _ask(
                "month",
                _item(_distribution("median"), "median"),
                _item({"kind": "period_to_date", "input": REVENUE, "period": "quarter"}, "qtd"),
            ),
            _data_months(f"{_per_order(MEDIAN)}, {QUARTER_TO_DATE}"),
        )
    )

    # Conversion within 7 days of signup (half-open), by the signup's bucket.
    rate = _item({"metric": "metric.shop.signup_to_order_7d"}, "rate")
    for grain in ("month", "quarter"):
        add(
            Case(
                f"conversion_by_{grain}",
                "utc_authored",
                _ask(grain, rate, role=SIGNUP_ROLE),
                CONVERSION.format(grain=grain),
            )
        )

    # Metric filters and filtered inputs.
    add(
        Case(
            "large_orders",
            "utc_authored",
            _ask("month", revenue, metric_filters=[LARGE_ORDER]),
            _by("month", "SUM(o.amount)", where="o.amount > 5"),
        )
    )
    add(
        Case(
            "repeat_customers",
            "utc_authored",
            _ask("quarter", revenue, _item(ORDERS, "orders"), metric_filters=[REPEAT_CUSTOMER]),
            _by(
                "quarter", "SUM(o.amount), COUNT(*)", where=f"o.customer_id IN ({REPEAT_CUSTOMERS})"
            ),
        )
    )
    store_a = {"all": [{"field": STORE, "op": "=", "value": "a"}]}
    add(
        Case(
            "filtered_input",
            "utc_authored",
            _ask(
                "quarter",
                revenue,
                _item({"kind": "aggregate", "measure": REVENUE["measure"], "filter": store_a}, "a"),
            ),
            _by(
                "quarter",
                "SUM(o.amount), SUM(CASE WHEN o.store_id = 'a' THEN o.amount END)",
            ),
        )
    )

    # Rollup routing: the monthly rollup (DATE keys) must answer exactly as the base table.
    for variant in ("utc_authored", "utc_implicit", "date_authored", "tz_authored"):
        clock = variant.split("_")[0]
        for grain in ("month", "quarter", "year"):
            add(
                Case(
                    f"{variant}-rollup_by_{grain}_and_store",
                    variant,
                    _ask(grain, revenue, _item(ORDERS, "orders"), group_by=[STORE]),
                    _by(grain, "SUM(o.amount), COUNT(*)", clock=clock, keys="o.store_id"),
                    routes=True,
                )
            )
    add(
        Case(
            "rollup_beside_base_average",
            "utc_authored",
            _ask("month", revenue, _item(AVERAGE, "average")),
            _by("month", "SUM(o.amount), AVG(o.amount)"),
        )
    )
    add(
        Case(
            "rollup_skipped_for_a_zone",
            "ny_authored",
            _ask("month", revenue),
            _by("month", "SUM(o.amount)", clock="ny"),
            routes=False,
        )
    )
    for name, bounds, routes in (
        ("rollup_month_bounds", ("2024-01-01", "2024-07-01"), True),
        ("rollup_skipped_for_mid_month_bounds", ("2024-01-15", "2024-06-15"), False),
    ):
        add(
            Case(
                name,
                "utc_authored",
                _ask("month", revenue, start=bounds[0], end=bounds[1]),
                _by(
                    "month",
                    "SUM(o.amount)",
                    where=f"o.ordered_at >= TIMESTAMP '{bounds[0]}'"
                    f" AND o.ordered_at < TIMESTAMP '{bounds[1]}'",
                ),
                routes=routes,
            )
        )

    # Dense per-store series: every store (NULL included) in every month.
    trailing = "(SELECT COALESCE(SUM(m.v), 0) FROM m WHERE m.k IS NOT DISTINCT FROM k.k"
    store_prior = STORE_NOW.replace("m.b = s.b", "m.b = s.b - INTERVAL '1 month'")
    add(
        Case(
            "prior_month_by_store",
            "utc_authored",
            _ask("month", revenue, _item(_prior("month"), "prior"), group_by=[STORE]),
            _by_store(
                f"{STORE_NOW}, CASE WHEN s.b = (SELECT MIN(b) FROM s) THEN NULL ELSE"
                f" {store_prior} END"
            ),
            known=BOTH(NULL_SUM_TO_ZERO),
        )
    )
    add(
        Case(
            "trailing_3_months_by_store",
            "utc_implicit",
            _ask("month", _item(_rolling("month", 3), "trailing"), group_by=[STORE]),
            _by_store(f"{trailing} AND m.b <= s.b AND m.b > s.b - INTERVAL '3 month')"),
        )
    )
    add(
        Case(
            "fill_by_store",
            "utc_authored",
            _ask(
                "month", revenue, group_by=[STORE], start="2023-11-01", end="2024-08-01", fill=True
            ),
            _by_store(STORE_NOW, bounds=("2023-11-01", "2024-07-01")),
            known=BOTH(NULL_SUM_TO_ZERO),
        )
    )

    # Filled windows that do not densify on their own.
    add(
        Case(
            "filled_cumulative_and_quarter_to_date",
            "utc_authored",
            _ask(
                "month",
                _item({"kind": "cumulative", "input": REVENUE}, "cumulative"),
                _item({"kind": "period_to_date", "input": REVENUE, "period": "quarter"}, "qtd"),
                fill=True,
            ),
            _series(
                "month",
                "(SELECT SUM(m.v) FROM m WHERE m.b <= s.b),"
                " (SELECT COALESCE(SUM(m.v), 0) FROM m WHERE m.b <= s.b"
                " AND date_trunc('quarter', m.b) = date_trunc('quarter', s.b))",
            ),
        )
    )
    add(
        Case(
            "distribution_revenue_and_prior",
            "utc_authored",
            _ask(
                "month",
                revenue,
                _item(_distribution("median"), "median"),
                _item(_prior("month"), "prior"),
            ),
            _series("month", f"{NOW}, {_per_order(MEDIAN)}, {_at('1 month')}"),
            known={
                "duckdb": GAP_NULL,
                "postgres": f"{DUPLICATE_PERIODS}, then {GAP_NULL}",
                "agree": DUPLICATE_PERIODS,
            },
        )
    )
    add(
        Case(
            "prior_and_trailing_two_quarters",
            "utc_implicit",
            _ask(
                "quarter",
                _item(_prior("quarter", 2), "prior"),
                _item(_rolling("quarter", 2), "trailing"),
            ),
            _series("quarter", f"{_at('6 month')}, {_trailing('6 month')}"),
        )
    )
    add(
        Case(
            "ny_prior_week",
            "ny_implicit",
            _ask("week", revenue, _item(_prior("week"), "prior")),
            _series("week", f"{NOW}, {_at('7 day')}", clock="ny"),
            known=BOTH(NULL_SUM_TO_ZERO),
        )
    )

    # A ratio and a conversion rate stay NULL in a month without a denominator.
    add(
        Case(
            "filled_ratio",
            "utc_authored",
            _ask("month", _item({"metric": "metric.shop.average_order_value"}, "aov"), fill=True),
            _series(
                "month",
                "(SELECT 1.0 * SUM(o.amount) / COUNT(*) FROM orders AS o"
                " WHERE date_trunc('month', o.ordered_at) = s.b)",
            ),
        )
    )
    add(
        Case(
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
    )
    add(
        Case(
            "big_customer_months",
            "utc_authored",
            _ask(
                "month",
                revenue,
                metric_filters=[_predicate("entity.shop_customer", "contextual", REVENUE, ">", 7)],
            ),
            _by(
                "month",
                "SUM(o.amount)",
                where="EXISTS (SELECT 1 FROM orders AS x WHERE x.customer_id = o.customer_id"
                " AND date_trunc('month', x.ordered_at) = date_trunc('month', o.ordered_at)"
                " HAVING SUM(x.amount) > 7)",
            ),
        )
    )
    return cases


CASES = _cases()
BACKENDS = ("duckdb", "postgres")


def _params(check: str, backends: tuple[str, ...]) -> list[Any]:
    params = []
    for case in CASES:
        for backend in backends:
            reason = case.known.get(check if check == "agree" else backend)
            marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
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
    if isinstance(left, date) and isinstance(right, date):
        return left == right
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
    return [tuple(row.values()) for row in result["rows"]]


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
