from __future__ import annotations

import pytest

from semantic_rails.compiler import compile_query, plan_comparison_bundle
from semantic_rails.registry import Registry

VALIDATION_MATRIX = [
    {
        "name": "duplicate_output_alias_rejected",
        "query": {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "dup"},
                {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "dup"},
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
        "error_code": "DUPLICATE_OUTPUT_ALIAS",
    },
    {
        "name": "time_only_missing_temporal_role_rejected",
        "query": {
            "version": 1,
            "time": {"grain": "month"},
        },
        "error_code": "INVALID_QUERY",
    },
    {
        "name": "cumulative_without_time_rejected",
        "query": {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.cumulative_revenue"},
                    "as": "cumulative_revenue",
                },
            ],
        },
        "error_code": "INVALID_QUERY",
    },
    {
        "name": "cumulative_bounded_start_rejected",
        "query": {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.sales.cumulative_revenue"},
                    "as": "cumulative_revenue",
                },
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "start": "2017-01-01 00:00:00",
                "end": "2017-04-01 00:00:00",
            },
        },
        "error_code": "CUMULATIVE_TIME_FILTER_UNSUPPORTED",
    },
    {
        "name": "rolling_window_grain_mismatch_rejected",
        "query": {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "rolling",
                        "input": {"measure": "measure.jaffle.order_count"},
                        "window": {"unit": "day", "value": 7},
                    },
                    "as": "orders_7d",
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
        "error_code": "REWRITE_NOT_SUPPORTED",
    },
    {
        "name": "prior_period_grain_mismatch_rejected",
        "query": {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "prior_period",
                        "input": {"measure": "measure.jaffle.order_count"},
                        "offset": {"unit": "day", "value": 7},
                    },
                    "as": "orders_prev_7d",
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
        "error_code": "REWRITE_NOT_SUPPORTED",
    },
]


QUERY_MATRIX = [
    {
        "name": "group_by_only_distinct_values_executes",
        "query": {
            "version": 1,
            "group_by": ["dimension.jaffle_store_name"],
            "order_by": [{"field": "dimension.jaffle_store_name", "direction": "ASC"}],
            "limit": 5,
        },
        "expected_columns": ["dimension.jaffle_store_name"],
    },
    {
        "name": "time_only_distinct_values_executes",
        "query": {
            "version": 1,
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "order_by": [{"field": "time", "direction": "ASC"}],
            "limit": 5,
        },
        "expected_columns": ["temporal_role.jaffle_order_time__month"],
    },
]


COMPILE_MATRIX = [
    {
        "name": "mixed_grain_rewrite_applies",
        "query": {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "measure": "measure.jaffle.order_count",
                        "aggregation": "count_distinct",
                    },
                    "as": "orders",
                },
                {
                    "expression": {
                        "measure": "measure.jaffle.item_count",
                        "aggregation": "count_distinct",
                    },
                    "as": "items",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
        "expected_fanout_status": "rewritten",
    },
]


@pytest.mark.parametrize("case", VALIDATION_MATRIX, ids=lambda case: case["name"])
def test_metricflow_validation_regression_matrix(runtime_factory, case):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(case["query"])

        assert report["ok"] is False
        assert report["errors"][0]["code"] == case["error_code"]
    finally:
        runtime.close()


@pytest.mark.parametrize("case", QUERY_MATRIX, ids=lambda case: case["name"])
def test_metricflow_query_regression_matrix(runtime_factory, case):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query(case["query"])

        assert result["row_count"] > 0
        assert set(case["expected_columns"]).issubset(result["rows"][0].keys())
    finally:
        runtime.close()


@pytest.mark.parametrize("case", COMPILE_MATRIX, ids=lambda case: case["name"])
def test_metricflow_compile_regression_matrix(package_config_factory, case):
    config, _ = package_config_factory("jaffle_shop")

    compiled = compile_query(config, Registry(config), case["query"])

    assert compiled["logical_plan"].fanout_strategy["status"] == case["expected_fanout_status"]


def test_metricflow_comparison_bundle_uses_same_query_for_same_clock_family(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")

    result = plan_comparison_bundle(
        config,
        Registry(config),
        left_object_id="metric.sales.food_revenue_share",
        right_object_id="metric.sales.drink_revenue_share",
        partial_query={"version": 1},
    )

    assert result["comparison_mode"] == "same_query"
    assert result["same_query_candidate"] is not None
    assert [row["object_id"] for row in result["comparison_bundle"]] == [
        "metric.sales.food_revenue_share",
        "metric.sales.drink_revenue_share",
    ]


def test_metricflow_comparison_bundle_coordinates_mixed_clock_metrics(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")

    result = plan_comparison_bundle(
        config,
        Registry(config),
        left_object_id="measure.jaffle.delivered_revenue_usd",
        right_object_id="measure.jaffle.revenue_usd",
        partial_query={
            "version": 1,
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
    )

    assert result["comparison_mode"] == "coordinated_queries"
    assert result["same_query_candidate"] is None
    assert result["blocked_same_query"] is None
    assert [row["query_patch"]["time"]["temporal_role"] for row in result["comparison_bundle"]] == [
        "temporal_role.jaffle_lifecycle_delivered_at",
        "temporal_role.jaffle_order_time",
    ]
    combined = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
                    "as": "delivered_revenue",
                },
                {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue_usd"},
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
    )
    assert "FULL OUTER JOIN leaf_2 AS right_side" in combined["sql"]
