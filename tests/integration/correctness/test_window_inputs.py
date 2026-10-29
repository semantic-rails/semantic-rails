"""Window answers checked against independent SQL; unsafe windows refuse."""

import pytest
import yaml

from semantic_rails.errors import SemanticLayerError

from .answer_ledger import load_entries
from .conftest import _runtime, _write_variant
from .test_correctness import (
    AVERAGE,
    CLOCK,
    CUSTOMER,
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
    key = "o.store_id" if by_store else "1"
    select_key = "keys.k, " if by_store else ""
    bucket = f"date_trunc('month', {CLOCK[clock]})"
    lookback = f" AND {bucket} > s.b - INTERVAL '3 month'" if kind == "rolling" else ""
    window = f"{key} IS NOT DISTINCT FROM keys.k AND {bucket} <= s.b{lookback}"
    # Each window's own orders, counted independently of their amount: one order (store a's,
    # in May 2024) has a NULL amount, so a window whose only order it is reads NULL, not 0.
    reference = f"""
        WITH m AS (SELECT {key} AS k, {bucket} AS b FROM orders o GROUP BY 1, 2),
        s AS (SELECT g.b FROM generate_series((SELECT MIN(b) FROM m),
              (SELECT MAX(b) FROM m), INTERVAL '1 month') AS g(b)),
        keys AS (SELECT DISTINCT k FROM m)
        SELECT {select_key}s.b,
          (SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE SUM(o.amount) END FROM orders o
           WHERE {window})
          / NULLIF((SELECT COUNT(*) FROM orders o WHERE {window}), 0)
        FROM keys CROSS JOIN s
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
    window = "{0} <= s.b AND {0} > s.b - INTERVAL '3 month'".format
    ordered, refunded = (window(f"date_trunc('month', {o}.ordered_at)") for o in ("o", "ro"))
    # One sum over each window's rows: 0 without any, NULL when every amount in it is NULL. A
    # shipping refund leaves goods_amount NULL, so goods refunded over December 2023 to
    # February 2024, whose only refund is order 4's shipping refund, is unknown.
    refunds = (
        " - (SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE SUM(r.goods_amount) END FROM refunds r"
        f" JOIN orders ro ON ro.order_id = r.order_id WHERE {refunded})"
        if form == "difference"
        else ""
    )
    scale = {"multiply": " * 2", "divide": " / 100.0"}.get(form, "")
    reference = f"""
        WITH s AS (SELECT g.b FROM generate_series(
          (SELECT date_trunc('month', MIN(ordered_at)) FROM orders),
          (SELECT date_trunc('month', MAX(ordered_at)) FROM orders), INTERVAL '1 month') AS g(b))
        SELECT s.b, ((SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE SUM(o.amount) END FROM orders o
          WHERE {ordered}){refunds}){scale} FROM s
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


@pytest.mark.parametrize("case_id", ["ratio_over_prior_month", "divide_by_prior_month"])
def test_a_ratio_over_a_prior_period_refuses_without_a_day_spine(packages, case_id):
    """ClickHouse has no implicit calendar to list the empty months, so a ratio over a prior
    period refuses as division does, instead of reading only the months with orders."""
    runtime = _runtime(packages["utc_implicit"], warehouse="clickhouse")
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.compile(dict(load_entries())[f"shop/{case_id}"]["query"])
    finally:
        runtime.close()
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert str(raised.value) == (
        "Warehouse 'clickhouse' has no implicit calendar, so time.fill, rolling and "
        "prior_period are not supported on it yet"
    )
    assert raised.value.details == {"warehouse": "clickhouse", "rewrite": "day_series"}


@pytest.mark.parametrize("form", ["ratio", "divide"])
def test_a_distribution_of_a_ratio_over_a_prior_period_refuses(request, form):
    """Each customer's revenue over their prior month needs every customer in every month, so
    a distribution of it would count customers in months they have no rows: refused, whether
    the input divides as a ratio or as arithmetic."""
    runtime = _backend(request, "duckdb").runtimes["utc_authored"]
    prior = {"kind": "prior_period", "input": REVENUE, "offset": {"unit": "month", "value": 1}}
    inputs = {
        "ratio": {"kind": "ratio", "numerator": REVENUE, "denominator": prior},
        "divide": {"kind": "arithmetic", "op": "divide", "left": REVENUE, "right": prior},
    }
    over = {"kind": "entity_value", "entity": "entity.shop_customer", "input": inputs[form]}
    query = _ask("month", _item({"kind": "distribution", "function": "median", "over": over}, "m"))
    with pytest.raises(SemanticLayerError) as raised:
        runtime.compile(query)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert str(raised.value) == (
        "A distribution is not supported when its input or a metric filter has a rolling or "
        "prior-period window: it would count entities in periods where they have no rows."
    )


# Each summing window, the call's item and the reference's condition on an order's bucket ``b``
# for the window that ends in the series bucket ``s.b``.
SUMMING_WINDOWS = {
    "rolling": (
        {"kind": "rolling", "input": REVENUE, "window": {"unit": "month", "value": 3}},
        "{b} <= s.b AND {b} > s.b - INTERVAL '3 month'",
    ),
    "cumulative": ({"kind": "cumulative", "input": REVENUE}, "{b} <= s.b"),
    "quarter_to_date": (
        {"kind": "period_to_date", "input": REVENUE, "period": "quarter"},
        "{b} <= s.b AND date_trunc('quarter', {b}) = date_trunc('quarter', s.b)",
    ),
}


def _window_reference(key: str, window: str) -> str:
    """Revenue over each key's window, as one sum of the window's own orders: 0 when it has
    none, NULL when every amount in it is NULL. Every key with orders, in every month from the
    first month with orders to the last."""
    bucket = "date_trunc('month', o.ordered_at)"
    return f"""
        WITH m AS (SELECT {key} AS k, {bucket} AS b FROM orders o GROUP BY 1, 2),
        s AS (SELECT g.b FROM generate_series((SELECT MIN(b) FROM m),
              (SELECT MAX(b) FROM m), INTERVAL '1 month') AS g(b)),
        keys AS (SELECT DISTINCT k FROM m)
        SELECT keys.k, s.b,
          (SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE SUM(o.amount) END FROM orders o
           WHERE {key} IS NOT DISTINCT FROM keys.k AND {window.format(b=bucket)})
        FROM keys CROSS JOIN s
    """


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("window", list(SUMMING_WINDOWS))
@pytest.mark.parametrize("by", [STORE, CUSTOMER], ids=["store", "customer"])
def test_summing_window_over_unknown_amounts_reads_null(request, backend_name, window, by):
    """Each store's (or customer's) trailing three months, revenue to date and quarter to date.
    Store a's only order in April to July 2024 and customer 105's only order (order 7, May) have
    no amount: a window whose orders all lack one reads NULL, as one sum over them would, never
    the 0 its months without orders add up to. A window without orders still reads 0."""
    backend = _backend(request, backend_name)
    expression, condition = SUMMING_WINDOWS[window]
    query = _ask("month", _item(expression, "value"), fill=True, group_by=[by])
    key = "o.store_id" if by == STORE else "o.customer_id"
    reference = _window_reference(key, condition)
    case = Case(f"{window}_over_unknown_amounts", "utc_authored", query, reference)
    expected = backend.reference(reference)
    unknown = sorted((str(row[0]), row[1].date().isoformat()) for row in expected if row[2] is None)
    assert (
        unknown
        == {
            ("rolling", STORE): [("a", "2024-06-01"), ("a", "2024-07-01")],
            ("cumulative", STORE): [],
            ("quarter_to_date", STORE): [("a", "2024-05-01"), ("a", "2024-06-01")],
            ("rolling", CUSTOMER): [
                ("105", "2024-05-01"),
                ("105", "2024-06-01"),
                ("105", "2024-07-01"),
            ],
            ("cumulative", CUSTOMER): [("105", f"2024-0{month}-01") for month in range(5, 9)],
            ("quarter_to_date", CUSTOMER): [("105", "2024-05-01"), ("105", "2024-06-01")],
        }[window, by]
    )
    _assert_rows(expected, _answer(backend, case), case.name)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_having_reads_a_window_over_unknown_amounts_as_null(request, backend_name):
    """ "Which stores' trailing three months have unknown revenue?": exactly store a's June and
    July 2024, whose only order has no amount, and no month whose window has no orders."""
    backend = _backend(request, backend_name)
    expression, condition = SUMMING_WINDOWS["rolling"]
    query = _ask(
        "month",
        _item(expression, "trailing"),
        group_by=[STORE],
        having=[{"field": "trailing", "op": "missing", "value": True}],
    )
    rows = backend.runtimes["utc_authored"].query(query)["rows"]
    assert [(row[STORE], row["period"], row["trailing"]) for row in rows] == [
        ("a", "2024-06-01", None),
        ("a", "2024-07-01", None),
    ]


def test_having_on_an_unselected_window_metric_reads_its_null(tmp_path):
    """A package's trailing-revenue metric read only by a `having` ("store-months whose trailing
    revenue is unknown") counts each period's rows as the selected window does: exactly store
    a's June and July 2024."""
    package = _write_variant(tmp_path, "utc_authored")
    path = package / "metrics" / "metrics.yml"
    metrics = yaml.safe_load(path.read_text(encoding="utf-8"))
    expression, _ = SUMMING_WINDOWS["rolling"]
    metrics["metrics"]["shop.trailing_revenue"] = {
        "as": "metric.shop.trailing_revenue",
        "label": "Trailing revenue",
        "kind": "derived",
        "value_type": "currency",
        "expression": {**expression, "input": {"kind": "aggregate", "measure": REVENUE}},
    }
    path.write_text(yaml.safe_dump(metrics, sort_keys=False), encoding="utf-8")
    runtime = _runtime(package)
    try:
        missing = {"field": "metric.shop.trailing_revenue", "op": "missing", "value": True}
        rows = runtime.query(_ask("month", REVENUE, group_by=[STORE], having=[missing]))["rows"]
    finally:
        runtime.close()
    assert [(row[STORE], row["period"]) for row in rows] == [
        ("a", "2024-06-01"),
        ("a", "2024-07-01"),
    ]
