"""Window answers checked against independent SQL; unsafe windows refuse."""

import pytest
import yaml

from semantic_rails.errors import SemanticLayerError

from .answer_ledger import load_entries
from .conftest import _runtime, _write_variant
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
        "metric": "metric.shop.average_order_value",
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
@pytest.mark.parametrize("form", ["flow", "difference", "multiply", "divide"])
def test_rolling_additive_arithmetic_keeps_its_totals(request, backend_name, form):
    backend = _backend(request, backend_name)
    inputs = {
        "flow": REVENUE,
        "difference": {"kind": "arithmetic", "op": "subtract", "left": REVENUE, "right": GOODS},
        "multiply": {
            "kind": "arithmetic",
            "op": "multiply",
            "left": REVENUE,
            "right": {"kind": "literal", "value": 2},
        },
        "divide": {
            "kind": "arithmetic",
            "op": "divide",
            "left": REVENUE,
            "right": {"kind": "literal", "value": 100},
        },
    }
    expression = {
        "kind": "rolling",
        "input": inputs[form],
        "window": {"unit": "month", "value": 3},
    }
    query = _ask("month", _item(expression, "trailing"))
    refunds = (
        " - COALESCE((SELECT SUM(r.goods_amount) FROM refunds r JOIN orders ro "
        "ON ro.order_id = r.order_id WHERE date_trunc('month', ro.ordered_at) = s.b), 0)"
        if form == "difference"
        else ""
    )
    scale = {"multiply": " * 2", "divide": " / 100.0"}.get(form, "")
    reference = f"""
        WITH s AS (SELECT g.b FROM generate_series(
          (SELECT date_trunc('month', MIN(ordered_at)) FROM orders),
          (SELECT date_trunc('month', MAX(ordered_at)) FROM orders), INTERVAL '1 month') AS g(b)),
        m AS (SELECT s.b, COALESCE((SELECT SUM(o.amount) FROM orders o
              WHERE date_trunc('month', o.ordered_at) = s.b), 0){refunds} AS v FROM s)
        SELECT b, SUM(v) OVER (ORDER BY b ROWS BETWEEN 2 PRECEDING AND CURRENT ROW){scale} FROM m
    """
    case = Case("rolling_flow", "utc_authored", query, reference)
    _assert_rows(backend.reference(reference), _answer(backend, case), case.name)


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


def test_an_authored_ratio_over_a_prior_period_reads_every_period(tmp_path):
    """A package metric written as a ratio over the prior month answers as the inline ratio
    frozen in shop/ratio_over_prior_month: March 2024 follows a month with no orders, so it
    reads NULL, never March over January."""
    package = _write_variant(tmp_path, "utc_authored")
    path = package / "metrics" / "metrics.yml"
    metrics = yaml.safe_load(path.read_text(encoding="utf-8"))
    revenue = {"kind": "aggregate", "measure": REVENUE}
    prior = {"kind": "prior_period", "input": revenue, "offset": {"unit": "month", "value": 1}}
    metrics["metrics"]["shop.revenue_vs_prior_month"] = {
        "as": "metric.shop.revenue_vs_prior_month",
        "label": "Revenue vs prior month",
        "kind": "derived",
        "value_type": "number",
        "expression": {"kind": "ratio", "numerator": revenue, "denominator": prior},
    }
    path.write_text(yaml.safe_dump(metrics, sort_keys=False), encoding="utf-8")
    runtime = _runtime(package)
    try:
        authored = runtime.query(_ask("month", "metric.shop.revenue_vs_prior_month"))
        inline = runtime.query(dict(load_entries())["shop/ratio_over_prior_month"]["query"])
    finally:
        runtime.close()
    rows = {row["period"]: row["revenue_vs_prior_month"] for row in authored["rows"]}
    assert rows == {row["period"]: row["vs_prior"] for row in inline["rows"]}
    assert rows["2024-02-01"] == 0 and rows["2024-03-01"] is None
