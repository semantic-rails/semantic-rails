from __future__ import annotations

from datetime import date

from semantic_rails.expressions import AggregateExpr, RollingExpr
from semantic_rails.metadata import _expr_summary, catalog_payload, valid_values_payload
from semantic_rails.runtime import Runtime

PACKAGE_CASES = {
    "jaffle_shop": {
        "query": {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
                {"expression": {"metric": "metric.sales.aov_usd"}, "as": "aov_usd"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "order_by": [{"field": "orders", "direction": "DESC"}],
            "limit": 5,
        },
        "valid_values_dimension": "dimension.jaffle_store_name",
        "expected_root": "entity.jaffle_order",
    },
}


def test_runtime_flows_work_on_real_duckdb_data(runtime_factory):
    for package_id, case in PACKAGE_CASES.items():
        runtime = runtime_factory(package_id)
        try:
            catalog = catalog_payload(runtime)
            validated = runtime.validate(case["query"])
            compiled = runtime.compile(case["query"])
            result = runtime.query(case["query"])
            values = valid_values_payload(runtime, dimension_id=case["valid_values_dimension"])

            assert catalog["entities"]
            assert validated["ok"] is True
            assert "rewrite_strategy" in compiled["explain"]
            assert result["row_count"] > 0
            assert values["values"]
        finally:
            runtime.close()


def test_distinct_value_queries_execute_on_real_data(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        grouped = {
            "version": 1,
            "group_by": ["dimension.jaffle_store_name"],
            "order_by": [{"field": "dimension.jaffle_store_name", "direction": "ASC"}],
            "limit": 5,
        }
        time_only = {
            "version": 1,
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "start": "2017-01-01 00:00:00",
                "end": "2017-04-01 00:00:00",
            },
            "order_by": [{"field": "time", "direction": "ASC"}],
        }
        joined_filter = {
            "version": 1,
            "group_by": ["dimension.jaffle_store_name"],
            "where": [
                {"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"},
            ],
            "order_by": [{"field": "dimension.jaffle_store_name", "direction": "ASC"}],
            "limit": 5,
        }

        grouped_result = runtime.query(grouped)
        time_only_result = runtime.query(time_only)
        joined_filter_result = runtime.query(joined_filter)

        assert grouped_result["row_count"] > 0
        assert set(grouped_result["rows"][0].keys()) == {"dimension.jaffle_store_name"}
        assert time_only_result["row_count"] > 0
        assert set(time_only_result["rows"][0].keys()) == {"temporal_role.jaffle_order_time__month"}
        assert joined_filter_result["row_count"] > 0
        assert set(joined_filter_result["rows"][0].keys()) == {"dimension.jaffle_store_name"}
    finally:
        runtime.close()


def test_expanded_jaffle_metrics_execute_on_real_data(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        multi_fact = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
                {
                    "expression": {"measure": "measure.jaffle.item_revenue_usd"},
                    "as": "item_revenue_usd",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 5,
        }
        product_margin = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.item_margin_usd"},
                    "as": "product_margin",
                },
            ],
            "group_by": ["dimension.jaffle_product_type"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 5,
        }
        new_customer_orders = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.new_customer_order_count"},
                    "as": "new_customer_orders",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 5,
        }

        multi_fact_result = runtime.query(multi_fact)
        product_margin_result = runtime.query(product_margin)
        new_customer_result = runtime.query(new_customer_orders)

        assert multi_fact_result["row_count"] > 0
        assert "item_revenue_usd" in multi_fact_result["rows"][0]
        assert product_margin_result["row_count"] > 0
        assert "product_margin" in product_margin_result["rows"][0]
        assert new_customer_result["row_count"] > 0
        assert "new_customer_orders" in new_customer_result["rows"][0]
    finally:
        runtime.close()


def test_canonical_jaffle_metric_family_executes_on_real_data(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        order_family = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.large_order_count"},
                    "as": "large_orders",
                },
                {"expression": {"measure": "measure.jaffle.food_order_count"}, "as": "food_orders"},
                {
                    "expression": {"measure": "measure.jaffle.drink_order_count"},
                    "as": "drink_orders",
                },
                {
                    "expression": {"measure": "measure.jaffle.gross_order_value_usd"},
                    "as": "gross_order_value",
                },
                {"expression": {"measure": "measure.jaffle.tax_paid_usd"}, "as": "tax_paid"},
                {"expression": {"measure": "measure.jaffle.order_cost_usd"}, "as": "order_cost"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 3,
        }
        share_family = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.food_revenue_usd"},
                    "as": "food_revenue",
                },
                {
                    "expression": {"measure": "measure.jaffle.drink_revenue_usd"},
                    "as": "drink_revenue",
                },
                {
                    "expression": {"metric": "metric.sales.food_revenue_share"},
                    "as": "food_revenue_share",
                },
                {
                    "expression": {"metric": "metric.sales.drink_revenue_share"},
                    "as": "drink_revenue_share",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 3,
        }
        customer_family = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.customer_count"},
                    "as": "customer_count",
                },
                {
                    "expression": {"measure": "measure.jaffle.lifetime_spend_before_tax_usd"},
                    "as": "lifetime_spend_before_tax",
                },
                {
                    "expression": {"measure": "measure.jaffle.lifetime_spend_usd"},
                    "as": "lifetime_spend",
                },
                {
                    "expression": {"measure": "measure.jaffle.lifetime_order_count"},
                    "as": "lifetime_order_count",
                },
                {
                    "expression": {"metric": "metric.sales.average_customer_lifetime_value"},
                    "as": "average_order_value",
                },
            ],
            "group_by": ["dimension.jaffle_customer_type"],
            "time": {
                "temporal_role": "temporal_role.jaffle_customer_first_order_at",
                "grain": "month",
            },
            "limit": 3,
        }
        supply_family = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.item_revenue_usd"},
                    "as": "product_revenue",
                },
                {
                    "expression": {"measure": "measure.jaffle.item_margin_usd"},
                    "as": "product_margin",
                },
                {
                    "expression": {"measure": "measure.jaffle.item_cost_usd"},
                    "as": "supply_cost_exposure",
                },
            ],
            "group_by": ["dimension.jaffle_product_type"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 3,
        }
        component_family = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.supply_component_count"},
                    "as": "supply_component_count",
                },
                {
                    "expression": {"measure": "measure.jaffle.supply_component_cost_usd"},
                    "as": "supply_component_cost",
                },
            ],
            "group_by": ["dimension.jaffle_product_type"],
            "limit": 3,
        }
        cumulative = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.cumulative_revenue"},
                    "as": "cumulative_revenue",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 3,
        }

        order_result = runtime.query(order_family)
        share_result = runtime.query(share_family)
        customer_result = runtime.query(customer_family)
        supply_result = runtime.query(supply_family)
        component_result = runtime.query(component_family)
        cumulative_result = runtime.query(cumulative)

        assert order_result["row_count"] > 0
        assert "large_orders" in order_result["rows"][0]
        assert "gross_order_value" in order_result["rows"][0]
        assert "tax_paid" in order_result["rows"][0]
        assert share_result["row_count"] > 0
        assert "food_revenue_share" in share_result["rows"][0]
        assert "drink_revenue_share" in share_result["rows"][0]
        assert customer_result["row_count"] > 0
        assert "lifetime_spend_before_tax" in customer_result["rows"][0]
        assert "lifetime_order_count" in customer_result["rows"][0]
        assert "average_order_value" in customer_result["rows"][0]
        assert supply_result["row_count"] > 0
        assert "product_revenue" in supply_result["rows"][0]
        assert "supply_cost_exposure" in supply_result["rows"][0]
        assert component_result["row_count"] > 0
        assert "supply_component_cost" in component_result["rows"][0]
        assert cumulative_result["row_count"] > 0
        assert "cumulative_revenue" in cumulative_result["rows"][0]
    finally:
        runtime.close()


def test_time_fill_returns_dense_daily_series(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2016-08-25 00:00:00",
                "end": "2016-09-03 00:00:00",
                "fill": True,
            },
            "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
        }

        result = runtime.query(query)

        assert result["row_count"] == 9
        assert result["rows"][0]["temporal_role.jaffle_order_time__day"] == date(2016, 8, 25)
        assert result["rows"][0]["orders"] == 0
        assert any(row["orders"] == 0 for row in result["rows"])
        assert any(row["orders"] > 0 for row in result["rows"])
    finally:
        runtime.close()


def test_time_axis_queries_accept_order_by_time_alias(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "start": "2017-01-01 00:00:00",
                "end": "2017-04-01 00:00:00",
            },
            "order_by": [{"field": "time", "direction": "ASC"}],
        }

        result = runtime.query(query)

        assert result["row_count"] > 0
        assert "temporal_role.jaffle_order_time__month" in result["rows"][0]
    finally:
        runtime.close()


def test_time_fill_supports_grouped_series(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2017-08-30 00:00:00",
                "end": "2017-09-03 00:00:00",
                "fill": True,
            },
            "order_by": [
                {"field": "dimension.jaffle_store_name", "direction": "ASC"},
                {"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"},
            ],
        }

        result = runtime.query(query)

        brooklyn_days = {
            row["temporal_role.jaffle_order_time__day"]
            for row in result["rows"]
            if row["dimension.jaffle_store_name"] == "Brooklyn"
        }
        assert brooklyn_days == {
            date(2017, 8, 30),
            date(2017, 8, 31),
            date(2017, 9, 1),
            date(2017, 9, 2),
        }
        assert any(
            row["dimension.jaffle_store_name"] == "Brooklyn" and row["orders"] == 0
            for row in result["rows"]
        )
    finally:
        runtime.close()


def test_extension_queries_execute_on_real_data(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        historical_segment_revenue = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.revenue_usd", "aggregation": "sum"},
                    "as": "revenue_usd",
                },
            ],
            "group_by": ["dimension.jaffle_customer_history_segment"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 5,
        }
        delivered_revenue = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
                    "as": "delivered_revenue",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_lifecycle_delivered_at",
                "grain": "month",
            },
            "limit": 5,
        }
        inventory_by_store = {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "measure": "measure.jaffle.inventory_on_hand_eop",
                        "aggregation": "last_value",
                    },
                    "as": "inventory_on_hand_eop",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
            "limit": 5,
        }

        historical_result = runtime.query(historical_segment_revenue)
        delivered_result = runtime.query(delivered_revenue)
        inventory_result = runtime.query(inventory_by_store)

        assert historical_result["row_count"] > 0
        assert "dimension.jaffle_customer_history_segment" in historical_result["rows"][0]
        assert delivered_result["row_count"] > 0
        assert "delivered_revenue" in delivered_result["rows"][0]
        assert inventory_result["row_count"] > 0
        assert "inventory_on_hand_eop" in inventory_result["rows"][0]
    finally:
        runtime.close()


def test_rolling_query_uses_dense_calendar_backed_series(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # Rolling queries must NOT be combined with a bounded `start` —
        # the leaf-level WHERE would truncate the lookback rows the
        # window function depends on (silent-wrong values). Drop `start`
        # so the window function sees the full lookback, then bound the
        # output via `end` + `limit`.
        rolling_query = {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "rolling",
                        "input": {"measure": "measure.jaffle.order_count"},
                        "window": {"unit": "day", "value": 3},
                    },
                    "as": "orders_3d",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "end": "2016-09-03 00:00:00",
                "fill": True,
            },
            "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "DESC"}],
            "limit": 9,
        }

        rolling_result = runtime.query(rolling_query)

        # The rolling sum has had access to the full lookback, so all
        # returned values are valid 3-day rolling sums of the underlying
        # daily order counts.
        assert rolling_result["row_count"] >= 1
        assert all(
            row["orders_3d"] is None or row["orders_3d"] >= 0 for row in rolling_result["rows"]
        )
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
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.rolling_7d_revenue_direct"},
                    "as": "v",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "start": "2017-01-08",
                "end": "2017-01-15",
            },
            "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
        }
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(rolling_query)
        assert excinfo.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"

        # Same shape but for kind: prior_period
        prior_period_query = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.prior_week_revenue_direct"},
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
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.revenue_mtd_direct"},
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
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.month_over_month_revenue_growth"},
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


def test_grouped_rolling_respects_partitions_with_fill(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # Bounded `start` is rejected (CRITICAL #1) — keep only `end`
        # so the window function sees the full lookback.
        rolling_query = {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "rolling",
                        "input": {"measure": "measure.jaffle.order_count"},
                        "window": {"unit": "day", "value": 2},
                    },
                    "as": "orders_2d",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "day",
                "end": "2017-09-03 00:00:00",
                "fill": True,
            },
            "order_by": [
                {"field": "dimension.jaffle_store_name", "direction": "ASC"},
                {"field": "temporal_role.jaffle_order_time__day", "direction": "DESC"},
            ],
            "limit": 50,
        }

        rolling_result = runtime.query(rolling_query)

        # Sanity-check: rolling values are non-negative integers and partitioned
        # by store (one rolling sum per store per day).
        assert rolling_result["row_count"] > 0
        stores_seen: set[str] = set()
        for row in rolling_result["rows"]:
            stores_seen.add(row["dimension.jaffle_store_name"])
            assert row["orders_2d"] is None or row["orders_2d"] >= 0
        assert len(stores_seen) >= 1
    finally:
        runtime.close()


def test_expr_summary_on_rolling_expression_does_not_crash():
    """Regression: ``_expr_summary`` must not crash on rolling expressions.

    Before this fix, ``semantic_rails/metadata.py:_expr_summary`` read
    ``expr.window.get('value')`` on a ``RollingExpr`` — but the dataclass
    has no ``.window`` attribute, it carries ``.unit`` and ``.value``
    directly. The first call to the summary against any rolling
    expression raised ``AttributeError``.
    """
    runtime = Runtime("jaffle_shop")
    try:
        rolling = RollingExpr(
            input=AggregateExpr(
                measure="measure.jaffle.revenue_usd",
                aggregation="sum",
            ),
            unit="day",
            value=7,
        )
        summary = _expr_summary(runtime.config, rolling)
        assert summary, "rolling summary must be non-empty"
        assert "rolling" in summary
        assert "7" in summary
        assert "day" in summary
    finally:
        runtime.close()


def test_prior_period_and_period_to_date_semantics_execute(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # Bounded `start` is rejected for windowed kinds (CRITICAL #1).
        # Use only `end` so the window function sees the full lookback.
        query = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
                {
                    "expression": {
                        "kind": "prior_period",
                        "input": {"measure": "measure.jaffle.order_count"},
                        "offset": {"unit": "month", "value": 1},
                    },
                    "as": "orders_prior_month",
                },
                {
                    "expression": {
                        "kind": "period_to_date",
                        "input": {"measure": "measure.jaffle.order_count"},
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
            "order_by": [{"field": "temporal_role.jaffle_order_time__month", "direction": "ASC"}],
        }

        result = runtime.query(query)

        expected_qtd = 0
        previous_quarter = None
        previous_orders = None
        for row in result["rows"]:
            month = row["temporal_role.jaffle_order_time__month"]
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


def test_published_time_semantic_and_snapshot_metrics_execute(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        jaffle_daily = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.rolling_28d_revenue"},
                    "as": "rolling_28d_revenue",
                },
                {"expression": {"metric": "metric.sales.revenue_qtd"}, "as": "revenue_qtd"},
                {"expression": {"metric": "metric.sales.revenue_ytd"}, "as": "revenue_ytd"},
            ],
            "time": {"temporal_role": "temporal_role.jaffle_daily_metric_day", "grain": "day"},
            "limit": 5,
        }
        jaffle_monthly = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.month_over_month_revenue_growth"},
                    "as": "month_over_month_revenue_growth",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 5,
        }
        jaffle_quarter_year = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
                {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue_usd"},
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "quarter"},
            "limit": 5,
        }
        jaffle_median = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.median_item_revenue"},
                    "as": "median_item_revenue",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_daily_metric_day", "grain": "day"},
            "limit": 5,
        }
        jaffle_percentile = {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "measure": "measure.jaffle.item_revenue_usd",
                        "aggregation": "percentile",
                        "parameters": {"p": 0.95},
                    },
                    "as": "p95_item_revenue",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 5,
        }

        jaffle_daily_result = runtime.query(jaffle_daily)
        jaffle_monthly_result = runtime.query(jaffle_monthly)
        jaffle_quarter_year_result = runtime.query(jaffle_quarter_year)
        jaffle_median_result = runtime.query(jaffle_median)
        jaffle_percentile_result = runtime.query(jaffle_percentile)

        assert jaffle_daily_result["row_count"] > 0
        assert "rolling_28d_revenue" in jaffle_daily_result["rows"][0]
        assert "revenue_qtd" in jaffle_daily_result["rows"][0]
        assert "revenue_ytd" in jaffle_daily_result["rows"][0]
        assert jaffle_monthly_result["row_count"] > 0
        assert "month_over_month_revenue_growth" in jaffle_monthly_result["rows"][0]
        assert jaffle_quarter_year_result["row_count"] > 0
        assert "temporal_role.jaffle_order_time__quarter" in jaffle_quarter_year_result["rows"][0]
        assert jaffle_median_result["row_count"] > 0
        assert "median_item_revenue" in jaffle_median_result["rows"][0]
        assert jaffle_percentile_result["row_count"] > 0
        assert "p95_item_revenue" in jaffle_percentile_result["rows"][0]
    finally:
        runtime.close()


def test_published_customer_segment_metrics_execute(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        customer_metrics = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.repeat_customer_count"},
                    "as": "repeat_customers",
                },
                {
                    "expression": {"measure": "measure.jaffle.high_value_customer_count"},
                    "as": "high_value_customers",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_customer_first_order_at",
                "grain": "month",
            },
            "limit": 5,
        }

        customer_result = runtime.query(customer_metrics)

        assert customer_result["row_count"] > 0
        assert "repeat_customers" in customer_result["rows"][0]
        assert "high_value_customers" in customer_result["rows"][0]
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
        "version": 1,
        "select": [
            {
                "expression": {"metric": "metric.sales.average_customer_lifetime_value"},
                "as": "avg_clv",
            },
        ],
    }
    compiled = compile_query(config, Registry(config), query)
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
            "version": 1,
            "select": [
                {
                    "expression": {
                        "measure": "measure.jaffle.lifetime_spend_usd",
                        "aggregation": "sum",
                    },
                    "as": "sum_spend",
                },
                {
                    "expression": {
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


def test_fiscal_calendar_buckets_leaf_by_calendar_columns(runtime_factory):
    """Regression test for CRITICAL #2: when calendar_id != default and
    fill: true is set, the leaf SQL must JOIN the calendar table and
    bucket by its grain columns (e.g. fiscal quarter_start), NOT by
    Gregorian DATE_TRUNC('quarter', ordered_at). The previous bug
    aligned the dense_time scaffold to fiscal but kept Gregorian
    bucketing in the leaf — every row joined to nothing and COALESCE
    filled all values with 0."""
    runtime = runtime_factory("jaffle_shop")
    try:
        fiscal_quarterly = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.revenue_usd", "aggregation": "sum"},
                    "as": "revenue_usd",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "calendar_id": "fiscal",
                "grain": "quarter",
                "fill": True,
                "start": "2017-01-01",
                "end": "2017-12-31",
            },
        }
        result = runtime.query(fiscal_quarterly)
        assert result["row_count"] >= 1
        # The bug returned all 0.0; now at least one fiscal quarter has
        # real revenue.
        nonzero = [row for row in result["rows"] if (row.get("revenue_usd") or 0) > 0]
        assert len(nonzero) >= 1, f"fiscal quarterly query returned all-zero rows: {result['rows']}"
        # Quarter starts must be fiscal anchors (Feb / May / Aug / Nov),
        # NOT Gregorian (Jan / Apr / Jul / Oct).
        quarter_starts = {row["temporal_role.jaffle_order_time__quarter"] for row in result["rows"]}
        assert any(getattr(qs, "month", None) in {2, 5, 8, 11} for qs in quarter_starts)
    finally:
        runtime.close()


def test_historical_snapshot_lifecycle_and_fiscal_extensions_execute(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        historical = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue_usd"},
            ],
            "group_by": ["dimension.jaffle_customer_history_segment"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "year"},
            "limit": 10,
        }
        snapshots = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.inventory_on_hand_sop"},
                    "as": "inventory_on_hand_sop",
                },
                {
                    "expression": {"metric": "metric.sales.inventory_on_hand_eop"},
                    "as": "inventory_on_hand_eop",
                },
                {
                    "expression": {"metric": "metric.sales.active_menu_count_eop"},
                    "as": "active_menu_count_eop",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
            "limit": 10,
        }
        lifecycle = {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.delivered_orders"},
                    "as": "delivered_orders",
                },
                {
                    "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
                    "as": "delivered_revenue",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {
                "temporal_role": "temporal_role.jaffle_lifecycle_delivered_at",
                "grain": "day",
            },
            "limit": 10,
        }
        fiscal = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "calendar_id": "fiscal",
                "fill": True,
                "start": "2016-09-01 00:00:00",
                "end": "2016-12-01 00:00:00",
            },
            "limit": 10,
        }

        historical_result = runtime.query(historical)
        snapshot_result = runtime.query(snapshots)
        lifecycle_result = runtime.query(lifecycle)
        fiscal_result = runtime.query(fiscal)

        assert historical_result["row_count"] > 0
        assert any(
            row["dimension.jaffle_customer_history_segment"] is None
            for row in historical_result["rows"]
        )
        assert any(
            row["dimension.jaffle_customer_history_segment"] == "repeat"
            for row in historical_result["rows"]
        )
        assert snapshot_result["row_count"] > 0
        assert "inventory_on_hand_sop" in snapshot_result["rows"][0]
        assert "inventory_on_hand_eop" in snapshot_result["rows"][0]
        assert lifecycle_result["row_count"] > 0
        assert "delivered_orders" in lifecycle_result["rows"][0]
        assert "delivered_revenue" in lifecycle_result["rows"][0]
        assert fiscal_result["row_count"] == 3
    finally:
        runtime.close()


def test_metric_predicate_metrics_and_conversion_execute(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        predicate_query = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.repeat_customer_orders"},
                    "as": "repeat_customer_orders",
                },
                {
                    "expression": {"metric": "metric.sales.high_value_customer_orders"},
                    "as": "high_value_customer_orders",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 10,
        }
        query_time_lifetime_predicate = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "metric_filters": [
                {
                    "expression": {
                        "kind": "metric_predicate",
                        "entity": "entity.jaffle_customer",
                        "scope_mode": "entity_only",
                        "input": {"measure": "measure.jaffle.lifetime_order_count"},
                        "op": ">",
                        "value": 1,
                    },
                    "op": "=",
                    "value": True,
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 10,
        }
        contextual_query_time_predicate = {
            "version": 1,
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
            "version": 1,
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
        monthly_store_contextual_metric = {
            "version": 1,
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
            "limit": 10,
        }
        daily_monthly_contextual_metric = {
            "version": 1,
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
            "order_by": [{"field": "time", "direction": "ASC"}],
            "limit": 10,
        }
        daily_query_on_month_metric = {
            "version": 1,
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
            "order_by": [{"field": "time", "direction": "ASC"}],
            "limit": 10,
        }
        conversion_query = {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.session_to_order_conversion_rate_7d"},
                    "as": "conversion_rate",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
        }

        predicate_result = runtime.query(predicate_query)
        query_time_lifetime_predicate_result = runtime.query(query_time_lifetime_predicate)
        contextual_query_time_predicate_result = runtime.query(contextual_query_time_predicate)
        contextual_metric_result = runtime.query(contextual_metric_query)
        monthly_store_contextual_metric_result = runtime.query(monthly_store_contextual_metric)
        daily_monthly_contextual_metric_result = runtime.query(daily_monthly_contextual_metric)
        daily_query_on_month_metric_result = runtime.query(daily_query_on_month_metric)
        same_store_conversion_query = {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
                    },
                    "as": "same_store_conversion_rate",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
            "limit": 10,
        }

        conversion_result = runtime.query(conversion_query)
        same_store_conversion_result = runtime.query(same_store_conversion_query)

        assert predicate_result["row_count"] > 0
        assert "repeat_customer_orders" in predicate_result["rows"][0]
        assert "high_value_customer_orders" in predicate_result["rows"][0]
        assert query_time_lifetime_predicate_result["row_count"] > 0
        assert "orders" in query_time_lifetime_predicate_result["rows"][0]
        assert contextual_query_time_predicate_result["row_count"] > 0
        assert contextual_metric_result["row_count"] > 0
        assert monthly_store_contextual_metric_result["row_count"] > 0
        assert daily_monthly_contextual_metric_result["row_count"] > 0
        assert daily_query_on_month_metric_result["row_count"] > 0
        assert "orders" in daily_monthly_contextual_metric_result["rows"][0]
        assert (
            daily_query_on_month_metric_result["rows"]
            == daily_monthly_contextual_metric_result["rows"]
        )
        assert sorted(
            contextual_query_time_predicate_result["rows"],
            key=lambda row: (
                row["dimension.jaffle_store_name"],
                row["temporal_role.jaffle_order_time__month"],
            ),
        ) == sorted(
            contextual_metric_result["rows"],
            key=lambda row: (
                row["dimension.jaffle_store_name"],
                row["temporal_role.jaffle_order_time__month"],
            ),
        )
        assert conversion_result["row_count"] > 0
        assert "conversion_rate" in conversion_result["rows"][0]
        assert same_store_conversion_result["row_count"] > 0
        assert "same_store_conversion_rate" in same_store_conversion_result["rows"][0]
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
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            ],
            "metric_filters": [
                {
                    "expression": {"measure": "measure.jaffle.item_count"},
                    "op": "=",
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
        leq_zero_query["metric_filters"] = [
            {
                "expression": {"measure": "measure.jaffle.item_count"},
                "op": "<=",
                "value": 0,
            }
        ]
        leq_zero_result = runtime.query(leq_zero_query)
        assert leq_zero_result["status"] == "ok"
        assert leq_zero_result["row_count"] == 1119

        # `>= 1` must still return the orders that DO have items
        # (59,652 total - 1,119 = 58,533).
        gte_one_query = dict(zero_items_query)
        gte_one_query["metric_filters"] = [
            {
                "expression": {"measure": "measure.jaffle.item_count"},
                "op": ">=",
                "value": 1,
            }
        ]
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
                "version": 2,
                "select": [
                    {
                        "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
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
