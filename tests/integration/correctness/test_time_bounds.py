"""Time bounds in the clock's zone and calendar, checked against independent SQL."""

from datetime import date

import pytest

from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.result_helpers import typed_rows

from .test_correctness import CLOCK, ORDERS, REVENUE, ROLE, STORE, _assert_rows, _backend, _item

INSTANTS = ("2024-07-01T02:00:00Z", "2024-06-30T22:00:00-04:00")
BOUNDS = {
    "utc": {
        "day": ("2024-06-30", "2024-07-01"),
        "month": ("2024-06-01", "2024-07-01"),
        "quarter": ("2024-04-01", "2024-07-01"),
    },
    "ny": {
        "day": ("2024-06-29", "2024-06-30"),
        "month": ("2024-05-01", "2024-06-01"),
        "quarter": ("2024-01-01", "2024-04-01"),
    },
}
BOUNDS["tz"] = BOUNDS["utc"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("clock", ["utc", "ny", "tz"])
@pytest.mark.parametrize("calendar", ["authored", "implicit"])
@pytest.mark.parametrize("unit", ["day", "month", "quarter"])
@pytest.mark.parametrize("shape", ["filled", "sparse", "total"])
def test_relative_bounds_use_one_local_date(request, backend_name, clock, calendar, unit, shape):
    backend = _backend(request, backend_name)
    runtime = backend.runtimes[f"{clock}_{calendar}"]
    start, end = BOUNDS[clock][unit]
    source = CLOCK[clock]
    within = f"{source} >= TIMESTAMP '{start}' AND {source} < TIMESTAMP '{end}'"
    if shape == "total":
        reference = (
            f"SELECT COALESCE(SUM(o.amount), 0) FROM orders o WHERE {within} HAVING COUNT(*) > 0"
        )
    elif shape == "sparse":
        reference = (
            f"SELECT date_trunc('{unit}', {source}), COALESCE(SUM(o.amount), 0) "
            f"FROM orders o WHERE {within} GROUP BY 1"
        )
    else:
        reference = (
            f"SELECT TIMESTAMP '{start}', COALESCE(SUM(o.amount), 0) FROM orders o WHERE {within}"
        )
    expected = backend.reference(reference)
    for instant in INSTANTS:
        result = runtime.query(
            {
                "select": [_item(REVENUE, "v")],
                "time": {
                    "temporal_role": ROLE,
                    "grain": "" if shape == "total" else unit,
                    "fill": shape == "filled",
                    "range": {"last": {"unit": unit, "value": 1}},
                },
                "policy_context": {"now": instant},
            }
        )
        resolved = result["normalized_query"]["time"]
        assert (resolved["start"], resolved["end"]) == (start, end)
        _assert_rows(expected, [tuple(r.values()) for r in typed_rows(result)], instant)
        # Sparse UTC month/quarter queries take the rollup; fill reads base coverage.
        if clock == "utc" and unit != "day" and shape == "sparse":
            assert "FROM orders_monthly" in result["rendered_sql"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("window", ["prior_period", "rolling"])
def test_relative_window_refusal_keeps_the_local_bounds(request, backend_name, window):
    backend = _backend(request, backend_name)
    expression = {"kind": window, "input": REVENUE}
    expression["offset" if window == "prior_period" else "window"] = {"unit": "month", "value": 1}
    for instant in INSTANTS:
        with pytest.raises(SemanticLayerError) as caught:
            backend.runtimes["ny_implicit"].query(
                {
                    "select": [_item(expression, "v")],
                    "time": {
                        "temporal_role": ROLE,
                        "grain": "month",
                        "range": {"last": {"unit": "month", "value": 1}},
                    },
                    "policy_context": {"now": instant},
                }
            )
        assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
        assert caught.value.details["start"] == "2024-05-01"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["utc_authored", "ny_authored"])
@pytest.mark.parametrize("shape", ["sparse", "filled", "total", "rolling"])
def test_fiscal_relative_periods_refuse_gregorian_floors(request, backend_name, variant, shape):
    backend = _backend(request, backend_name)
    expression = (
        REVENUE
        if shape != "rolling"
        else {"kind": "rolling", "input": REVENUE, "window": {"unit": "quarter", "value": 1}}
    )
    with pytest.raises(SemanticLayerError) as caught:
        backend.runtimes[variant].query(
            {
                "select": [_item(expression, "v")],
                "time": {
                    "temporal_role": ROLE,
                    "grain": "" if shape == "total" else "quarter",
                    "calendar_id": "fiscal",
                    "fill": shape == "filled",
                    "range": {"last": {"unit": "quarter", "value": 9}},
                },
                "policy_context": {"now": "2026-12-31T02:00:00Z"},
            }
        )
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.details["path"] == "time.range.last.unit"
    assert caught.value.details["calendar_id"] == "fiscal"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["date_authored", "date_implicit"])
@pytest.mark.parametrize("end", ["2024-07-01T03:00:00", "2024-07-01T00:00:00"])
@pytest.mark.parametrize("shape", ["sparse", "filled", "total", "folded", "dimensions"])
def test_date_source_and_spine_share_the_end_day(request, backend_name, variant, end, shape):
    backend = _backend(request, backend_name)
    source = "CAST(o.order_date AS TIMESTAMP)"
    within = f"{source} >= TIMESTAMP '2024-06-01' AND {source} < TIMESTAMP '{end}'"
    query = {
        "select": [] if shape == "dimensions" else [_item(REVENUE, "v")],
        "time": {
            "temporal_role": ROLE,
            "grain": "" if shape == "total" else "month",
            "start": "2024-06-01",
            "end": end,
            "fill": shape == "filled",
        },
    }
    if shape == "dimensions":
        query["group_by"] = [STORE]
        reference = f"SELECT o.store_id, date_trunc('month', {source}) FROM orders o WHERE {within} GROUP BY 1, 2"
    elif shape == "total":
        query["where"] = [{"field": STORE, "op": "=", "value": "b"}]
        reference = f"SELECT SUM(o.amount) FROM orders o WHERE {within} AND store_id = 'b'"
    else:
        columns = "SUM(o.amount)"
        if shape == "folded":
            query["select"].append(_item(ORDERS, "n"))
            columns += ", COUNT(*)"
        reference = f"SELECT date_trunc('month', {source}), {columns} FROM orders o WHERE {within} GROUP BY 1"
    result = backend.runtimes[variant].query(query)
    _assert_rows(
        backend.reference(reference), [tuple(r.values()) for r in typed_rows(result)], shape
    )
    if shape == "sparse" and end.endswith("00:00:00"):
        assert "FROM orders_monthly" in result["rendered_sql"]
    if not end.endswith("00:00:00"):
        assert "FROM orders_monthly" not in result["rendered_sql"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["date_authored", "date_implicit"])
@pytest.mark.parametrize("window", ["prior_period", "rolling"])
def test_date_window_and_observation_include_the_end_day(request, backend_name, variant, window):
    backend = _backend(request, backend_name)
    expression = {"kind": window, "input": REVENUE}
    expression["offset" if window == "prior_period" else "window"] = {
        "unit": "month",
        "value": 1 if window == "prior_period" else 2,
    }
    result = backend.runtimes[variant].query(
        {
            "select": [_item(REVENUE, "v"), _item(expression, "w")],
            "time": {"temporal_role": ROLE, "grain": "month", "end": "2024-07-01T03:00:00"},
        }
    )
    reference = backend.reference(
        "SELECT SUM(amount), (SELECT SUM(amount) FROM orders WHERE order_date >= DATE '2024-06-01' AND order_date < DATE '2024-07-01') "
        "FROM orders WHERE CAST(order_date AS TIMESTAMP) >= TIMESTAMP '2024-07-01' AND CAST(order_date AS TIMESTAMP) < TIMESTAMP '2024-07-01 03:00:00'"
    )[0]
    current, prior = reference
    rows = typed_rows(result)
    july = next(r for r in rows if r[f"{ROLE}__month"].date() == date(2024, 7, 1))
    assert july["v"] == current == 9
    assert july["w"] == (prior if window == "prior_period" else current + prior)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["date_authored", "date_implicit"])
@pytest.mark.parametrize("scope", ["contextual", "entity_only"])
def test_date_predicate_window_uses_the_source_day_rule(request, backend_name, variant, scope):
    backend = _backend(request, backend_name)
    predicate = {
        "kind": "metric_predicate",
        "entity": "entity.shop_customer",
        "scope_mode": scope,
        "input": ORDERS,
        "op": ">=",
        "value": 1,
    }
    if scope == "entity_only":
        predicate["time_alignment"] = "query_window"
    result = backend.runtimes[variant].query(
        {
            "select": [_item(REVENUE, "v")],
            "time": {
                "temporal_role": ROLE,
                "grain": "month",
                "start": "2024-06-01",
                "end": "2024-07-01T03:00:00",
            },
            "metric_filters": [{"expression": predicate, "op": "=", "value": True}],
        }
    )
    reference = backend.reference(
        "SELECT date_trunc('month', CAST(order_date AS TIMESTAMP)), SUM(amount) FROM orders "
        "WHERE CAST(order_date AS TIMESTAMP) >= TIMESTAMP '2024-06-01' AND CAST(order_date AS TIMESTAMP) < TIMESTAMP '2024-07-01 03:00:00' GROUP BY 1"
    )
    _assert_rows(reference, [tuple(r.values()) for r in typed_rows(result)], scope)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["date_authored", "date_implicit"])
@pytest.mark.parametrize(
    "start,end,day_predicate",
    [
        ("2024-07-01T12:00:00", "2024-07-02", "order_date = DATE '2024-07-01'"),
        (None, "2024-07-01T00:00:00.000000001", "order_date <= DATE '2024-07-01'"),
        ("2024-07-01T12:00:00", None, "order_date >= DATE '2024-07-01'"),
        (
            "2024-07-01T01:00:00-04:00",
            "2024-07-01T02:00:00-04:00",
            "order_date = DATE '2024-07-01'",
        ),
        ("2024-07-01T12:00:00", "2024-07-01T12:00:00", "FALSE"),
        ("2024-07-02", "2024-07-01T12:00:00", "FALSE"),
    ],
)
def test_date_bounds_touch_whole_days_including_open_windows(
    request, backend_name, variant, start, end, day_predicate
):
    backend = _backend(request, backend_name)
    result = backend.runtimes[variant].query(
        {
            "select": [_item(REVENUE, "v")],
            "time": {"temporal_role": ROLE, "grain": "month", "start": start, "end": end},
        }
    )
    reference = backend.reference(
        f"SELECT date_trunc('month', CAST(order_date AS TIMESTAMP)), SUM(amount) FROM orders WHERE {day_predicate} GROUP BY 1"
    )
    _assert_rows(reference, [tuple(r.values()) for r in typed_rows(result)], "whole days")


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_invalid_date_bound_is_refused_before_a_source_scan(request, backend_name):
    backend = _backend(request, backend_name)
    with pytest.raises(SemanticLayerError) as caught:
        backend.runtimes["date_implicit"].query(
            {"select": [_item(REVENUE, "v")], "time": {"temporal_role": ROLE, "end": "not-a-date"}}
        )
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.details["path"] == "time.end"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("leaf,query_clock", [("day", "time"), ("time", "day")])
def test_anchored_population_bounds_use_the_leaf_clock(request, backend_name, leaf, query_clock):
    backend = _backend(request, backend_name)
    population = {
        "kind": "scoped_aggregate",
        "measure": f"measure.shop.{leaf}_population",
        "aggregation": "count_distinct",
    }
    predicate = {
        "entity": "entity.shop_clock_edge",
        "measure": f"measure.shop.{query_clock}_amount",
        "op": "=",
        "value": 10,
        "time_alignment": "same_query_period",
    }
    result = backend.runtimes["utc_implicit"].query(
        {
            "select": [
                _item(
                    {
                        "kind": "ratio",
                        "numerator": {**population, "predicates": [predicate]},
                        "denominator": population,
                    },
                    "v",
                )
            ],
            "time": {
                "temporal_role": f"temporal_role.shop_clock_edge_source_{query_clock}",
                "grain": "month",
                "start": "2024-07-01T12:00:00",
                "end": "2024-07-02T03:00:00",
            },
        }
    )
    assert "latest_shop_clock_edge_snapshot" in result["rendered_sql"]
    expected = backend.reference(
        "SELECT TIMESTAMP '2024-07-01', "
        "COUNT(DISTINCT CASE WHEN amount = 10 THEN id END) * 1.0 / COUNT(DISTINCT id) "
        "FROM clock_edges"
    )
    assert expected[0][1] == 0.5
    _assert_rows(expected, [tuple(r.values()) for r in typed_rows(result)], leaf)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["utc_authored", "utc_implicit"])
@pytest.mark.parametrize("shape", ["sparse", "filled", "total"])
def test_converted_date_bounds_include_both_local_days(request, backend_name, variant, shape):
    backend = _backend(request, backend_name)
    amount = {"measure": "measure.shop.local_amount"}
    query = {
        "select": [_item(amount, "v")],
        "time": {
            "temporal_role": "temporal_role.shop_clock_edge_local_day",
            "grain": "" if shape == "total" else "day",
            "fill": shape == "filled",
            "start": "2024-06-30",
            "end": "2024-07-01T23:00:00",
        },
    }
    result = backend.runtimes[variant].query(query)
    source = "((CAST(source_day AS TIMESTAMP) AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York')"
    reference = (
        "SELECT SUM(amount) FROM clock_edges"
        if shape == "total"
        else f"SELECT date_trunc('day', {source}), SUM(amount) FROM clock_edges GROUP BY 1"
    )
    expected = backend.reference(reference)
    assert sorted(row[-1] for row in expected) == ([30] if shape == "total" else [10, 20])
    _assert_rows(expected, [tuple(r.values()) for r in typed_rows(result)], shape)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    "clock,measure", [("local_day", "local_amount"), ("source_day", "day_amount")]
)
def test_entity_only_predicate_window_refuses_timezone_conversion(
    request, backend_name, clock, measure
):
    backend = _backend(request, backend_name)
    role = f"temporal_role.shop_clock_edge_{clock}"
    amount = {"measure": f"measure.shop.{measure}"}
    query = {
        "select": [_item(amount, "v")],
        "time": {
            "temporal_role": role,
            "grain": "day",
            "start": "2024-06-30",
            "end": "2024-07-01T23:00:00",
        },
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.shop_clock_edge",
                    "input": amount,
                    "scope_mode": "entity_only",
                    "time_alignment": "query_window",
                    "op": ">=",
                    "value": 1,
                },
                "op": "=",
                "value": True,
            }
        ],
    }
    runtime = backend.runtimes["utc_implicit"]
    if clock == "local_day":
        with pytest.raises(SemanticLayerError) as caught:
            runtime.query(query)
        assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
        assert role in str(caught.value)
        assert caught.value.details["temporal_role"] == role
    else:
        result = runtime.query(query)
        expected = backend.reference(
            "SELECT CAST(source_day AS TIMESTAMP), SUM(amount) FROM clock_edges "
            "WHERE source_day >= DATE '2024-06-30' AND source_day <= DATE '2024-07-01' GROUP BY 1"
        )
        assert len(expected) == 1
        assert expected[0][0].date() == date(2024, 7, 1)
        assert expected[0][1] == 10
        _assert_rows(expected, [tuple(r.values()) for r in typed_rows(result)], clock)
