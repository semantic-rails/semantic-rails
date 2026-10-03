"""Window answers checked against independent SQL; unsupported calendars refuse."""

import pytest

from semantic_rails.errors import SemanticLayerError

from .test_correctness import (
    AVERAGE,
    CLOCK,
    GOODS,
    LARGEST,
    ORDERS,
    REVENUE,
    STORE,
    Case,
    _answer,
    _ask,
    _assert_rows,
    _backend,
    _distribution,
    _item,
)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("clock", ["utc", "ny"])
@pytest.mark.parametrize("kind", ["cumulative", "rolling"])
@pytest.mark.parametrize("by_store", [False, True], ids=["all", "store"])
@pytest.mark.parametrize("form", ["metric", "ratio", "divide"])
def test_average_order_window_matches_ratio_of_totals(
    request, backend_name, clock, kind, by_store, form
):
    backend = _backend(request, backend_name)
    inputs = {
        "metric": {"metric": "metric.shop.average_order_value"},
        "ratio": {"kind": "ratio", "numerator": REVENUE, "denominator": ORDERS},
        "divide": {"kind": "arithmetic", "op": "divide", "left": REVENUE, "right": ORDERS},
    }
    expression = {"kind": kind, "input": inputs[form]}
    if kind == "rolling":
        expression["window"] = {"unit": "month", "value": 3}
    query = _ask("month", _item(expression, "aov"), fill=True, group_by=[STORE] if by_store else [])
    frame = "2 PRECEDING" if kind == "rolling" else "UNBOUNDED PRECEDING"
    key = "o.store_id" if by_store else "1"
    partition = "PARTITION BY k " if by_store else ""
    select_key = "k, " if by_store else ""
    # Count orders independently of their amount: one order has a NULL amount.
    reference = f"""
        WITH m AS (
          SELECT {key} AS k, date_trunc('month', {CLOCK[clock]}) AS b,
                 COALESCE(SUM(o.amount), 0) AS revenue, COUNT(*) AS orders
          FROM orders o GROUP BY 1, 2
        ),
        s AS (SELECT g.b FROM generate_series((SELECT MIN(b) FROM m),
              (SELECT MAX(b) FROM m), INTERVAL '1 month') AS g(b)),
        keys AS (SELECT DISTINCT k FROM m),
        dense AS (
          SELECT keys.k, s.b, COALESCE(m.revenue, 0) AS revenue, COALESCE(m.orders, 0) AS orders
          FROM keys CROSS JOIN s LEFT JOIN m
            ON m.k IS NOT DISTINCT FROM keys.k AND m.b = s.b
        )
        SELECT {select_key}b,
          SUM(revenue) OVER ({partition}ORDER BY b ROWS BETWEEN {frame} AND CURRENT ROW)
          / NULLIF(SUM(orders) OVER ({partition}ORDER BY b ROWS BETWEEN {frame} AND CURRENT ROW), 0)
        FROM dense
    """
    case = Case("average_order_window", f"{clock}_authored", query, reference)
    _assert_rows(backend.reference(reference), _answer(backend, case), case.name)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("difference", [False, True], ids=["flow", "difference"])
def test_rolling_flow_and_difference_keep_their_totals(request, backend_name, difference):
    backend = _backend(request, backend_name)
    input_expr = {"kind": "arithmetic", "op": "subtract", "left": REVENUE, "right": GOODS}
    expression = {
        "kind": "rolling",
        "input": input_expr if difference else REVENUE,
        "window": {"unit": "month", "value": 3},
    }
    query = _ask("month", _item(expression, "trailing"))
    refunds = (
        " - COALESCE((SELECT SUM(r.goods_amount) FROM refunds r JOIN orders ro "
        "ON ro.order_id = r.order_id WHERE date_trunc('month', ro.ordered_at) = s.b), 0)"
        if difference
        else ""
    )
    reference = f"""
        WITH s AS (SELECT g.b FROM generate_series(
          (SELECT date_trunc('month', MIN(ordered_at)) FROM orders),
          (SELECT date_trunc('month', MAX(ordered_at)) FROM orders), INTERVAL '1 month') AS g(b)),
        m AS (SELECT s.b, COALESCE((SELECT SUM(o.amount) FROM orders o
              WHERE date_trunc('month', o.ordered_at) = s.b), 0){refunds} AS v FROM s)
        SELECT b, SUM(v) OVER (ORDER BY b ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM m
    """
    case = Case("rolling_flow", "utc_authored", query, reference)
    _assert_rows(backend.reference(reference), _answer(backend, case), case.name)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("clock", ["utc", "ny"])
@pytest.mark.parametrize("grain", ["month", "quarter"])
@pytest.mark.parametrize("period", ["year", "quarter"])
def test_fiscal_period_to_date_refuses_before_sql(
    request, backend_name, clock, grain, period, monkeypatch
):
    runtime = _backend(request, backend_name).runtimes[f"{clock}_authored"]
    expression = {"kind": "period_to_date", "input": REVENUE, "period": period}
    query = _ask(grain, _item(expression, "to_date"), calendar_id="fiscal", fill=True)
    monkeypatch.setattr(runtime, "_get_adapter", lambda: pytest.fail("refuse before SQL"))
    report = runtime.validate(query)
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "REWRITE_NOT_SUPPORTED"
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(query)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details["calendar_id"] == "fiscal"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    "input_expr", [AVERAGE, LARGEST, _distribution("median", "value")["expression"]]
)
def test_rolling_statistic_refuses_before_sql(request, backend_name, input_expr, monkeypatch):
    runtime = _backend(request, backend_name).runtimes["utc_authored"]
    query = _ask(
        "month",
        _item(
            {"kind": "rolling", "input": input_expr, "window": {"unit": "month", "value": 3}},
            "value",
        ),
    )
    monkeypatch.setattr(runtime, "_get_adapter", lambda: pytest.fail("refuse before SQL"))
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(query)
    assert raised.value.code == "ROLLUP_UNSAFE"
