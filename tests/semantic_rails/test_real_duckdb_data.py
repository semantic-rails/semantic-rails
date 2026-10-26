from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config


@pytest.mark.parametrize("temporal", [True, False], ids=["history-key", "co-located-key"])
def test_customer_key_grouping_matches_reference_sql(tmp_path, temporal):
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    dimension = (
        "dimension.jaffle_customer_history_customer_id"
        if temporal
        else "dimension.jaffle_customer_id"
    )
    month = "period"
    query = {
        "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
        "group_by": [dimension],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }
    reference = (
        "SELECT h.customer_id, DATE_TRUNC('month', o.ordered_at), COUNT(DISTINCT o.order_id)"
        " FROM jaffle_order o LEFT JOIN jaffle_customer_history h"
        " ON o.customer_id = h.customer_id AND o.ordered_at >= h.valid_from"
        " AND (o.ordered_at < h.valid_to OR h.valid_to IS NULL) GROUP BY 1, 2"
        if temporal
        else "SELECT customer_id, DATE_TRUNC('month', ordered_at), COUNT(DISTINCT order_id)"
        " FROM jaffle_order GROUP BY 1, 2"
    )
    runtime = Runtime.from_path(str(package))
    try:
        engine_query, _ = read_call(query, config=runtime.config)
        compiled = compile_query(runtime.config, runtime.registry, engine_query)
        result = runtime.query(query)
        with duckdb.connect(str(package / "jaffle_shop.duckdb"), read_only=True) as connection:
            expected = {
                (customer, bucket.date().isoformat()): orders
                for customer, bucket, orders in connection.execute(reference).fetchall()
            }
    finally:
        runtime.close()

    actual = {(row[dimension], row[month]): row["orders"] for row in result["rows"]}
    assert actual == expected
    assert result["row_count"] == len(expected) == (60 if temporal else 5782)
    if temporal:
        assert "LEFT JOIN jaffle_customer_history" in compiled["sql"]
    else:
        assert "JOIN" not in compiled["sql"]


def test_distinct_value_queries_execute_on_real_data(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        grouped = {
            "group_by": ["dimension.jaffle_store_name"],
            "order_by": [{"field": "dimension.jaffle_store_name", "direction": "asc"}],
            "limit": 5,
        }
        time_only = {
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "start": "2017-01-01 00:00:00",
                "end": "2017-04-01 00:00:00",
            },
            "order_by": [{"field": "period", "direction": "asc"}],
        }
        joined_filter = {
            "group_by": ["dimension.jaffle_store_name"],
            "where": [
                {"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"},
            ],
            "order_by": [{"field": "dimension.jaffle_store_name", "direction": "asc"}],
            "limit": 5,
        }

        grouped_result = runtime.query(grouped)
        time_only_result = runtime.query(time_only)
        joined_filter_result = runtime.query(joined_filter)

        assert grouped_result["row_count"] > 0
        assert set(grouped_result["rows"][0].keys()) == {"dimension.jaffle_store_name"}
        assert time_only_result["row_count"] > 0
        assert set(time_only_result["rows"][0].keys()) == {"period", "period_partial"}
        assert joined_filter_result["row_count"] > 0
        assert set(joined_filter_result["rows"][0].keys()) == {"dimension.jaffle_store_name"}
    finally:
        runtime.close()


def test_time_fill_returns_dense_daily_series(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "select": [
                {"expression": "measure.jaffle.order_count", "as": "orders"},
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2016-08-25 00:00:00",
                "end": "2016-09-03 00:00:00",
            },
            "order_by": [{"field": "period", "direction": "asc"}],
        }

        result = runtime.query(query)

        assert result["row_count"] == 9
        assert result["rows"][0]["period"] == "2016-08-25"
        # Orders are loaded from 2016-09-01: the days before have no data, not 0.
        assert [row["orders"] for row in result["rows"][:7]] == [None] * 7
        assert all(row["orders"] > 0 for row in result["rows"][7:])
    finally:
        runtime.close()


def test_time_fill_supports_grouped_series(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "select": [
                {"expression": "measure.jaffle.order_count", "as": "orders"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            # Brooklyn opens on 2017-03-12, inside the loaded orders.
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2017-03-10 00:00:00",
                "end": "2017-03-14 00:00:00",
            },
            "order_by": [
                {"field": "dimension.jaffle_store_name", "direction": "asc"},
                {"field": "period", "direction": "asc"},
            ],
        }

        result = runtime.query(query)

        brooklyn = {
            row["period"]: row["orders"]
            for row in result["rows"]
            if row["dimension.jaffle_store_name"] == "Brooklyn"
        }
        assert set(brooklyn) == {"2017-03-10", "2017-03-11", "2017-03-12", "2017-03-13"}
        assert brooklyn["2017-03-10"] == brooklyn["2017-03-11"] == 0
    finally:
        runtime.close()


def test_rolling_with_bounded_start_is_rejected(runtime_factory):
    """Regression test for CRITICAL #1: bounded `start` truncates the
    leaf-level rows the rolling window function depends on. Previously
    this silently returned ramp-up values that converged to the right
    answer only after `window` days inside the bounded range; now the
    planner rejects the query with WINDOWED_TIME_FILTER_UNSUPPORTED
    so the wrong values are never returned."""
    import pytest

    from semantic_rails.errors import SemanticLayerError

    runtime = runtime_factory("jaffle_shop")
    try:
        rolling_query = {
            "select": [
                {
                    "expression": "metric.sales.rolling_7d_revenue_direct",
                    "as": "v",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2017-01-08",
                "end": "2017-01-15",
            },
            "order_by": [{"field": "period", "direction": "asc"}],
        }
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(rolling_query)
        assert excinfo.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"

        # Same shape but for kind: prior_period
        prior_period_query = {
            "select": [
                {
                    "expression": "metric.sales.prior_week_revenue_direct",
                    "as": "v",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2017-01-08",
            },
        }
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(prior_period_query)
        assert excinfo.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"

        # Same shape but for kind: period_to_date
        ptd_query = {
            "select": [
                {
                    "expression": "metric.sales.revenue_mtd_direct",
                    "as": "v",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2017-01-08",
            },
        }
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(ptd_query)
        assert excinfo.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"

        # Derived metric containing prior_period (month-over-month growth)
        derived_query = {
            "select": [
                {
                    "expression": "metric.sales.month_over_month_revenue_growth",
                    "as": "mom",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "start": "2016-10-01",
            },
        }
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(derived_query)
        assert excinfo.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    finally:
        runtime.close()


def test_prior_period_and_period_to_date_semantics_execute(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # Bounded `start` is rejected for windowed kinds (CRITICAL #1).
        # Use only `end` so the window function sees the full lookback.
        query = {
            "select": [
                {"expression": "measure.jaffle.order_count", "as": "orders"},
                {
                    "expression": {
                        "kind": "prior_period",
                        "input": "measure.jaffle.order_count",
                        "offset": {"unit": "month", "value": 1},
                    },
                    "as": "orders_prior_month",
                },
                {
                    "expression": {
                        "kind": "period_to_date",
                        "input": "measure.jaffle.order_count",
                        "period": "quarter",
                    },
                    "as": "orders_qtd",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "end": "2018-02-01 00:00:00",
            },
            "order_by": [{"field": "period", "direction": "asc"}],
        }

        result = runtime.query(query)

        expected_qtd = 0
        previous_quarter = None
        previous_orders = None
        for row in result["rows"]:
            month = datetime.fromisoformat(row["period"])
            quarter_key = (month.year, (month.month - 1) // 3)
            if quarter_key != previous_quarter:
                expected_qtd = row["orders"]
            else:
                expected_qtd += row["orders"]
            if previous_orders is None:
                assert row["orders_prior_month"] is None
            else:
                assert row["orders_prior_month"] == previous_orders
            assert row["orders_qtd"] == expected_qtd
            previous_quarter = quarter_key
            previous_orders = row["orders"]
    finally:
        runtime.close()


def test_average_customer_lifetime_value_is_mean_not_ratio(runtime_factory, package_config_factory):
    """Regression test for HIGH #1: the metric label says
    'Average customer lifetime value' but the SQL previously computed
    `SUM(lifetime_spend) / SUM(lifetime_order_count)` — a weighted-mean
    revenue-per-order, not the mean LTV across customers. The metric
    is now `kind: derived` `AVG(lifetime_spend_usd)` which matches the
    name."""
    from semantic_rails.compiler import compile_query
    from semantic_rails.registry import Registry

    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {
                "expression": "metric.sales.average_customer_lifetime_value",
                "as": "avg_clv",
            },
        ],
    }
    compiled = compile_query(config, Registry(config), read_call(query, config=config)[0])
    sql = compiled["sql"].upper()
    assert "AVG(" in sql, f"expected AVG in SQL, got:\n{compiled['sql']}"
    # The previous SUM/SUM ratio shape would have two SUMs and a NULLIF —
    # the new shape has one AVG and no division.
    assert sql.count("SUM(") == 0, f"expected no SUM in SQL, got:\n{compiled['sql']}"

    runtime = runtime_factory("jaffle_shop")
    try:
        # Cross-check numerically: AVG(spend) over the customer population
        # differs from SUM(spend)/SUM(orders) when the population has
        # heterogeneous order counts. Drive both queries directly so we
        # don't depend on any other metric.
        avg_result = runtime.query(query)
        sum_ratio_query = {
            "select": [
                {
                    "expression": {
                        "kind": "measure",
                        "measure": "measure.jaffle.lifetime_spend_usd",
                        "aggregation": "sum",
                    },
                    "as": "sum_spend",
                },
                {
                    "expression": {
                        "kind": "measure",
                        "measure": "measure.jaffle.lifetime_order_count",
                        "aggregation": "sum",
                    },
                    "as": "sum_orders",
                },
            ],
        }
        sum_ratio_result = runtime.query(sum_ratio_query)
        avg_value = avg_result["rows"][0]["avg_clv"]
        sum_spend = sum_ratio_result["rows"][0]["sum_spend"]
        sum_orders = sum_ratio_result["rows"][0]["sum_orders"]
        ratio_value = sum_spend / sum_orders
        # The ratio (SUM/SUM) is revenue-per-order, ~$13–$15.
        # The mean of lifetime_spend across customers is much larger.
        assert abs(avg_value - ratio_value) > 1.0, (
            f"avg_clv ({avg_value}) too close to SUM/SUM ratio ({ratio_value}) — "
            "the metric may be computing the wrong thing"
        )
        assert avg_value > 100.0  # mean LTV should be hundreds, not single-digit dollars
    finally:
        runtime.close()


def test_fiscal_quarters_are_refused_not_bucketed_as_gregorian(runtime_factory):
    """A fiscal quarterly fill once read every value as 0, and leaf paths could
    bucket Gregorian quarters under fiscal labels. Authored non-default calendars
    are refused in this release, before any SQL runs."""
    runtime = runtime_factory("jaffle_shop")
    try:
        fiscal_quarterly = {
            "select": [
                {
                    "expression": {
                        "kind": "measure",
                        "measure": "measure.jaffle.revenue_usd",
                        "aggregation": "sum",
                    },
                    "as": "revenue_usd",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_fiscal_calendar_day",
                "grain": "quarter",
            },
        }
        for window in ({"start": "2017-01-01", "end": "2017-12-31"}, {"fill": True}):
            with pytest.raises(SemanticLayerError) as refused:
                runtime.query({**fiscal_quarterly, "time": {**fiscal_quarterly["time"], **window}})
            assert refused.value.code == "REWRITE_NOT_SUPPORTED"
            assert refused.value.details["reason"] == "calendar_not_supported_yet"
    finally:
        runtime.close()


def test_historical_segments_include_orders_without_a_valid_version(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        historical = {
            "select": [
                {"expression": "measure.jaffle.revenue_usd", "as": "revenue_usd"},
            ],
            "group_by": ["dimension.jaffle_customer_history_segment"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "year"},
            "limit": 10,
        }
        historical_result = runtime.query(historical)

        assert historical_result["row_count"] > 0
        assert any(
            row["dimension.jaffle_customer_history_segment"] is None
            for row in historical_result["rows"]
        )
        assert any(
            row["dimension.jaffle_customer_history_segment"] == "repeat"
            for row in historical_result["rows"]
        )
    finally:
        runtime.close()


def test_metric_predicate_metrics_execute(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        predicate_query = {
            "select": [
                "metric.sales.repeat_customer_orders",
                "metric.sales.high_value_customer_orders",
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 10,
        }
        contextual_query_time_predicate = {
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "metric_filters": [
                {
                    "expression": {
                        "kind": "metric_predicate",
                        "entity": "entity.jaffle_customer",
                        "input": {"measure": "measure.jaffle.order_count"},
                        "op": ">",
                        "value": 10,
                    },
                    "op": "=",
                    "value": True,
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 50,
        }
        contextual_metric_query = {
            "select": [
                {
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.order_count",
                        "aggregation": "count_distinct",
                        "predicates": [
                            {
                                "input": {"measure": "measure.jaffle.order_count"},
                                "entity": "entity.jaffle_customer",
                                "op": ">",
                                "value": 10,
                            }
                        ],
                    },
                    "as": "orders",
                }
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 50,
        }
        daily_monthly_contextual_metric = {
            "select": [
                {
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.order_count",
                        "aggregation": "count_distinct",
                        "predicates": [
                            {
                                "input": {"measure": "measure.jaffle.order_count"},
                                "entity": "entity.jaffle_customer",
                                "op": ">",
                                "value": 10,
                                "time_grain": "month",
                            }
                        ],
                    },
                    "as": "orders",
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            "order_by": [{"field": "period", "direction": "asc"}],
            "limit": 10,
        }
        predicate_result = runtime.query(predicate_query)
        # A predicate in each row's own context (or its own month) is the engine's form.
        internal = runtime.query_internal
        contextual_query_time_predicate_result = internal(contextual_query_time_predicate)
        contextual_metric_result = internal(contextual_metric_query)
        daily_monthly_contextual_metric_result = internal(daily_monthly_contextual_metric)

        assert predicate_result["row_count"] > 0
        assert "repeat_customer_orders" in predicate_result["rows"][0]
        assert "high_value_customer_orders" in predicate_result["rows"][0]
        assert contextual_query_time_predicate_result["row_count"] > 0
        assert contextual_metric_result["row_count"] > 0
        assert daily_monthly_contextual_metric_result["row_count"] > 0
        assert "orders" in daily_monthly_contextual_metric_result["rows"][0]
        assert sorted(
            contextual_query_time_predicate_result["rows"],
            key=lambda row: (
                row["dimension.jaffle_store_name"],
                row["period"],
            ),
        ) == sorted(
            contextual_metric_result["rows"],
            key=lambda row: (
                row["dimension.jaffle_store_name"],
                row["period"],
            ),
        )
    finally:
        runtime.close()


def test_metric_filter_zero_count_returns_orders_with_no_items(runtime_factory):
    """Regression for zero-count metric_filter on FULL OUTER JOIN combine.

    The natural translation of "orders with no items" is `metric_filter
    op: "=" value: 0` against the `item_count` measure. Before the COALESCE
    fix, the FULL OUTER JOIN combine left `item_count` as NULL for orders
    with no matching items, and `NULL = 0` evaluates to UNKNOWN, so the
    rows were silently dropped. The seed dataset has 1,119 such orders;
    this test pins that the natural form returns them.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        zero_items_query = {
            "select": [
                {"expression": "measure.jaffle.order_count", "as": "orders"},
            ],
            "having": [
                {
                    "field": "measure.jaffle.item_count",
                    "op": "is",
                    "value": 0,
                }
            ],
            "group_by": ["dimension.jaffle_order_id"],
        }
        result = runtime.query(zero_items_query)
        assert result["status"] == "ok"
        # Verified directly against the seed: 1,119 orders have no items.
        assert result["row_count"] == 1119

        # A `<= 0` filter must also return the same 1,119 orders — without
        # COALESCE, `NULL <= 0` is also UNKNOWN and would drop them.
        leq_zero_query = dict(zero_items_query)
        leq_zero_query["having"] = [{"field": "measure.jaffle.item_count", "op": "lte", "value": 0}]
        leq_zero_result = runtime.query(leq_zero_query)
        assert leq_zero_result["status"] == "ok"
        assert leq_zero_result["row_count"] == 1119

        # `>= 1` must still return the orders that DO have items
        # (59,652 total - 1,119 = 58,533).
        gte_one_query = dict(zero_items_query)
        gte_one_query["having"] = [{"field": "measure.jaffle.item_count", "op": "gte", "value": 1}]
        gte_one_result = runtime.query(gte_one_query)
        assert gte_one_result["status"] == "ok"
        assert gte_one_result["row_count"] == 58533
    finally:
        runtime.close()


def test_order_lifecycle_covers_full_order_date_range(runtime_factory):
    """Lifecycle rows derive delivered_at for every order, so delivered-grain
    rollups span the same date range as ordered-grain ones. Guards the demo's
    "delivered revenue by month" preset, which previously collapsed to the
    single hand-authored day in raw_order_lifecycle_events.csv."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query(
            {
                "select": [
                    {
                        "expression": "measure.jaffle.delivered_revenue_usd",
                        "as": "delivered_revenue",
                    },
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_lifecycle_delivered_at",
                    "grain": "month",
                },
            }
        )
    finally:
        runtime.close()
    assert result["row_count"] >= 12


def test_dense_fill_does_not_fabricate_zeros_for_non_additive_measures(runtime_factory):
    """Densifying invents rows; it must not invent measurements.

    `SUM` over no rows is 0, so filling is right. `MIN` over no rows is
    undefined — a filled `0` is a fabricated minimum that sits below every
    value actually observed, and nothing in the envelope marks it as
    synthetic. Both measures ride the same dense spine here, so the two
    fills have to differ within a single result set.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "select": [
                {
                    "expression": {
                        "kind": "measure",
                        "measure": "measure.jaffle.lifetime_spend_before_tax_usd",
                        "aggregation": "min",
                    },
                    "as": "min_spend",
                },
                {
                    "expression": {
                        "kind": "measure",
                        "measure": "measure.jaffle.lifetime_order_count",
                        "aggregation": "sum",
                    },
                    "as": "orders",
                },
            ],
            # Of these loaded days, only 2016-10-02 has a customer's first order.
            "time": {
                "temporal_role": "temporal_role.jaffle_customer_first_order_at",
                "grain": "day",
                "start": "2016-10-01 00:00:00",
                "end": "2016-10-15 00:00:00",
            },
        }

        result = runtime.query(query)
        rows = result["rows"]
        empty = [row for row in rows if row["orders"] == 0]
        populated = [row for row in rows if row["orders"] > 0]

        assert empty, "the loaded range must hold empty buckets"
        assert populated, "range must also cover buckets that carry data"

        for row in empty:
            assert row["min_spend"] is None, f"empty bucket reported a fabricated minimum: {row!r}"

        # The additive measure keeps its identity fill.
        assert all(row["orders"] == 0 for row in empty)

        # And the fabricated value would have been detectable as wrong:
        # every real minimum is strictly above zero.
        assert all(row["min_spend"] > 0 for row in populated)
    finally:
        runtime.close()


def test_dense_fill_still_zero_fills_additive_measures(runtime_factory):
    """Guard the other direction: the fix must not turn every fill into NULL."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # Brooklyn opens in March 2017, so it has no orders in the loaded January and February.
        query = {
            "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
            "where": [{"field": "dimension.jaffle_store_name", "op": "is", "value": "Brooklyn"}],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "start": "2017-01-01 00:00:00",
                "end": "2017-05-01 00:00:00",
            },
        }

        rows = runtime.query(query)["rows"]
        assert any(row["orders"] == 0 for row in rows)
        assert not any(row["orders"] is None for row in rows)
    finally:
        runtime.close()
