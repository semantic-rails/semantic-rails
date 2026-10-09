from __future__ import annotations

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry
from tests.integration.correctness.answer_ledger import load_entries
from tests.integration.correctness.conftest import SHOP

SNAPSHOT_CASES = {
    "conversion": (
        {
            "limit": 5,
            "select": [
                {
                    "as": "conversion_rate",
                    "expression": {"metric": "metric.sales.session_to_order_conversion_rate_7d"},
                }
            ],
            "time": {"grain": "day", "temporal_role": "temporal_role.jaffle_session_started_at"},
        },
        'WITH conversion_leaf_1__conversion_base_1 AS (\nSELECT\n  DATE_TRUNC(\'day\', CAST(jaffle_storefront_session.started_at AS TIMESTAMP)) AS t,\n  jaffle_storefront_session.started_at AS __base_event_time,\n  jaffle_storefront_session.session_id AS __base_event_key,\n  jaffle_customer.customer_id AS __match_key_1\nFROM jaffle_storefront_session\nINNER JOIN jaffle_customer ON jaffle_storefront_session.customer_id = jaffle_customer.customer_id\n),\nconversion_leaf_1__conversion_converted_1 AS (\nSELECT\n  jaffle_order.ordered_at AS __converted_event_time,\n  jaffle_order.order_id AS __converted_event_key,\n  jaffle_customer.customer_id AS __match_key_1\nFROM jaffle_order\nINNER JOIN jaffle_customer ON jaffle_order.customer_id = jaffle_customer.customer_id\n),\nconversion_leaf_1__conversion_matches_1 AS (\nSELECT\n  base_events.t AS t,\n  base_events.__base_event_key AS __base_event_key,\n  converted_events.__converted_event_key AS __converted_event_key,\n  ROW_NUMBER() OVER (PARTITION BY base_events.__base_event_key ORDER BY converted_events.__converted_event_time ASC, converted_events.__converted_event_key ASC) AS __match_rank\nFROM conversion_leaf_1__conversion_base_1 AS base_events\nLEFT JOIN conversion_leaf_1__conversion_converted_1 AS converted_events ON converted_events.__converted_event_time >= base_events.__base_event_time AND CAST(converted_events.__converted_event_time AS TIMESTAMP) < DATE_ADD(CAST(base_events.__base_event_time AS TIMESTAMP), INTERVAL (7) DAY) AND base_events.__match_key_1 IS NOT DISTINCT FROM converted_events.__match_key_1\n),\nconversion_leaf_1 AS (\nSELECT\n  matches.t AS t,\n  COUNT(DISTINCT CASE WHEN matches.__converted_event_key IS NOT NULL THEN matches.__base_event_key END) / NULLIF(COUNT(DISTINCT matches.__base_event_key), 0) AS m1\nFROM conversion_leaf_1__conversion_matches_1 AS matches\nWHERE\n  matches.__match_rank = 1\nGROUP BY\n  matches.t\n)\nSELECT\n  base.t AS "temporal_role.jaffle_session_started_at__day",\n  base.m1 AS conversion_rate\nFROM conversion_leaf_1 AS base\nORDER BY\n  "temporal_role.jaffle_session_started_at__day" ASC,\n  conversion_rate ASC NULLS LAST\nLIMIT 5',
    ),
    "cumulative": (
        {
            "select": [
                {
                    "as": "cumulative_revenue",
                    "expression": {"metric": "metric.sales.cumulative_revenue"},
                }
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        "WITH leaf_1 AS (\nSELECT\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  SUM(jaffle_order.order_total_cents / 100.0) AS m1,\n  COUNT(1) AS m1_rows\nFROM jaffle_order\nGROUP BY\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1\nFROM leaf_1 AS base\n)\nSELECT\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  SUM(base.m1) OVER (ORDER BY base.t ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_revenue\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC",
    ),
    "dense_fill": (
        {
            "select": [
                {
                    "as": "prior_year_orders",
                    "expression": {
                        "input": {"measure": "measure.jaffle.order_count"},
                        "kind": "prior_period",
                        "offset": {"unit": "year", "value": 1},
                    },
                },
                {
                    "as": "orders_qtd",
                    "expression": {
                        "input": {"measure": "measure.jaffle.order_count"},
                        "kind": "period_to_date",
                        "period": "quarter",
                    },
                },
            ],
            "time": {
                "calendar_id": "default",
                "grain": "month",
                "temporal_role": "temporal_role.jaffle_order_time",
            },
        },
        "WITH leaf_1 AS (\nSELECT\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  COUNT(DISTINCT jaffle_order.order_id) AS m1\nFROM jaffle_order\nGROUP BY\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nleaf_base AS (\nSELECT\n  base.t AS t,\n  base.m1 AS m1\nFROM leaf_1 AS base\n),\ndense_bounds AS (\nSELECT\n  MIN(leaf_base.t) AS range_start,\n  MAX(leaf_base.t) AS range_end\nFROM leaf_base\n),\nimplicit_days AS (\nSELECT\n  CAST(day_series.series_day AS DATE) AS date_day\nFROM dense_bounds\nCROSS JOIN LATERAL GENERATE_SERIES(CAST(dense_bounds.range_start AS DATE), CAST(dense_bounds.range_end AS DATE), INTERVAL (1) DAY) AS day_series(series_day)\n),\nimplicit_calendar AS (\nSELECT\n  implicit_days.date_day AS date_day,\n  DATE_TRUNC('month', CAST(implicit_days.date_day AS TIMESTAMP)) AS bucket\nFROM implicit_days\n),\ncalendar_time AS (\nSELECT\n  implicit_calendar.bucket AS t\nFROM implicit_calendar\nCROSS JOIN dense_bounds\nWHERE\n  implicit_calendar.bucket >= dense_bounds.range_start\n  AND implicit_calendar.bucket <= dense_bounds.range_end\nGROUP BY\n  implicit_calendar.bucket\n),\nleaf_time_keys AS (\nSELECT\n  leaf_base.t AS t,\n  1 AS source_present\nFROM leaf_base\nWHERE\n  leaf_base.t IS NOT NULL\nGROUP BY\n  leaf_base.t\n),\ndense_time AS (\nSELECT\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END AS t\nFROM calendar_time\nFULL OUTER JOIN leaf_time_keys ON calendar_time.t = leaf_time_keys.t\nGROUP BY\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END\n),\nseries_base AS (\nSELECT\n  dense_time.t AS t,\n  leaf_base.m1 AS m1\nFROM dense_time\nLEFT JOIN leaf_base ON dense_time.t = leaf_base.t\n),\ncoverage_1 AS (\nSELECT\n  MIN(DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))) AS loaded_from,\n  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(jaffle_order.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) END) AS loaded_to\nFROM jaffle_order\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(NULLIF(base.m1, 0), CASE WHEN MAX(base.m1) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1\nFROM series_base AS base\nCROSS JOIN coverage_1\n)\nSELECT\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  LAG(base.m1, 12) OVER (ORDER BY base.t ASC) AS prior_year_orders,\n  SUM(base.m1) OVER (PARTITION BY DATE_TRUNC('quarter', CAST(base.t AS TIMESTAMP)) ORDER BY base.t ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS orders_qtd\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC",
    ),
    "metric_predicate": (
        {
            "group_by": ["dimension.jaffle_store_name"],
            "select": [
                {
                    "as": "revenue_from_ordering_customers",
                    "expression": {
                        "aggregation": "sum",
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.revenue_usd",
                        "predicates": [
                            {
                                "entity": "entity.jaffle_customer",
                                "input": {"measure": "measure.jaffle.order_count"},
                                "op": ">",
                                "time_alignment": "same_query_period",
                                "value": 0,
                            }
                        ],
                    },
                }
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        'WITH leaf_1__order_count_customer_month_source_1__leaf_1 AS (\nSELECT\n  jaffle_order.customer_id AS g1,\n  jaffle_order.store_id AS g2,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  COUNT(DISTINCT jaffle_order.order_id) AS m1\nFROM jaffle_order\nGROUP BY\n  jaffle_order.customer_id,\n  jaffle_order.store_id,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nleaf_1__order_count_customer_month_source_1__guarded_base AS (\nSELECT\n  base.g1 AS g1,\n  base.g2 AS g2,\n  base.t AS t,\n  CASE WHEN MAX(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1\nFROM leaf_1__order_count_customer_month_source_1__leaf_1 AS base\n),\nleaf_1__order_count_customer_month_source_1 AS (\nSELECT\n  base.g1 AS "dimension.jaffle_customer_id",\n  base.g2 AS "dimension.jaffle_store_id",\n  base.t AS t,\n  base.m1 AS __predicate_value\nFROM leaf_1__order_count_customer_month_source_1__guarded_base AS base\n),\nleaf_1__qualified_customers_month_by_order_count_1 AS (\nSELECT DISTINCT\n  predicate_source."dimension.jaffle_customer_id" AS "dimension.jaffle_customer_id",\n  predicate_source."dimension.jaffle_store_id" AS "dimension.jaffle_store_id",\n  predicate_source.t AS t\nFROM leaf_1__order_count_customer_month_source_1 AS predicate_source\nWHERE\n  predicate_source.__predicate_value > 0\n),\nleaf_1 AS (\nSELECT\n  jaffle_store.store_name AS g1,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  SUM(jaffle_order.order_total_cents / 100.0) AS m1,\n  COUNT(1) AS m1_rows\nFROM jaffle_order\nLEFT JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id\nINNER JOIN leaf_1__qualified_customers_month_by_order_count_1 ON jaffle_order.customer_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_customer_id" AND jaffle_store.store_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_store_id" AND DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP)) = leaf_1__qualified_customers_month_by_order_count_1.t\nGROUP BY\n  jaffle_store.store_name,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nguarded_base AS (\nSELECT\n  base.g1 AS g1,\n  base.t AS t,\n  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1\nFROM leaf_1 AS base\n)\nSELECT\n  base.g1 AS "dimension.jaffle_store_name",\n  base.t AS "temporal_role.jaffle_order_time__month",\n  base.m1 AS revenue_from_ordering_customers\nFROM guarded_base AS base\nORDER BY\n  "temporal_role.jaffle_order_time__month" ASC,\n  "dimension.jaffle_store_name" ASC',
    ),
    "mixed_grain_rewrite": (
        {
            "group_by": ["dimension.jaffle_store_name"],
            "select": [
                {
                    "as": "orders",
                    "expression": {
                        "aggregation": "count_distinct",
                        "measure": "measure.jaffle.order_count",
                    },
                },
                {
                    "as": "items",
                    "expression": {
                        "aggregation": "count_distinct",
                        "measure": "measure.jaffle.item_count",
                    },
                },
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        "WITH leaf_1 AS (\nSELECT\n  jaffle_store.store_name AS g1,\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  COUNT(DISTINCT jaffle_order.order_id) AS m1\nFROM jaffle_order\nLEFT JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id\nGROUP BY\n  jaffle_store.store_name,\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nleaf_2 AS (\nSELECT\n  jaffle_store.store_name AS g1,\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  COUNT(DISTINCT jaffle_item.item_id) AS m2\nFROM jaffle_item\nINNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id\nLEFT JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id\nGROUP BY\n  jaffle_store.store_name,\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\ncombined_2 AS (\nSELECT\n  COALESCE(left_side.g1, right_side.g1) AS g1,\n  COALESCE(left_side.t, right_side.t) AS t,\n  left_side.m1 AS m1,\n  right_side.m2 AS m2\nFROM leaf_1 AS left_side\nFULL OUTER JOIN leaf_2 AS right_side ON left_side.g1 IS NOT DISTINCT FROM right_side.g1 AND left_side.t IS NOT DISTINCT FROM right_side.t\n),\ncoverage_1 AS (\nSELECT\n  MIN(DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))) AS loaded_from,\n  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(jaffle_order.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) END) AS loaded_to\nFROM jaffle_order\n),\nguarded_base AS (\nSELECT\n  base.g1 AS g1,\n  base.t AS t,\n  COALESCE(NULLIF(base.m1, 0), CASE WHEN MAX(base.m1) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1,\n  COALESCE(NULLIF(base.m2, 0), CASE WHEN MAX(base.m2) OVER () > 0 THEN 0 END) AS m2\nFROM combined_2 AS base\nCROSS JOIN coverage_1\n)\nSELECT\n  base.g1 AS \"dimension.jaffle_store_name\",\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  base.m1 AS orders,\n  base.m2 AS items\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC,\n  \"dimension.jaffle_store_name\" ASC",
    ),
    "period_to_date": (
        {
            "select": [
                {
                    "as": "orders_qtd",
                    "expression": {
                        "input": {"measure": "measure.jaffle.order_count"},
                        "kind": "period_to_date",
                        "period": "quarter",
                    },
                }
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        "WITH leaf_1 AS (\nSELECT\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  COUNT(DISTINCT jaffle_order.order_id) AS m1\nFROM jaffle_order\nGROUP BY\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  CASE WHEN MAX(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1\nFROM leaf_1 AS base\n)\nSELECT\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  SUM(base.m1) OVER (PARTITION BY DATE_TRUNC('quarter', CAST(base.t AS TIMESTAMP)) ORDER BY base.t ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS orders_qtd\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC",
    ),
    "prior_period": (
        {
            "select": [
                {
                    "as": "prior_year_orders",
                    "expression": {
                        "input": {"measure": "measure.jaffle.order_count"},
                        "kind": "prior_period",
                        "offset": {"unit": "year", "value": 1},
                    },
                }
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        "WITH leaf_1 AS (\nSELECT\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  COUNT(DISTINCT jaffle_order.order_id) AS m1\nFROM jaffle_order\nGROUP BY\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nleaf_base AS (\nSELECT\n  base.t AS t,\n  base.m1 AS m1\nFROM leaf_1 AS base\n),\ndense_bounds AS (\nSELECT\n  MIN(leaf_base.t) AS range_start,\n  MAX(leaf_base.t) AS range_end\nFROM leaf_base\n),\nimplicit_days AS (\nSELECT\n  CAST(day_series.series_day AS DATE) AS date_day\nFROM dense_bounds\nCROSS JOIN LATERAL GENERATE_SERIES(CAST(dense_bounds.range_start AS DATE), CAST(dense_bounds.range_end AS DATE), INTERVAL (1) DAY) AS day_series(series_day)\n),\nimplicit_calendar AS (\nSELECT\n  implicit_days.date_day AS date_day,\n  DATE_TRUNC('month', CAST(implicit_days.date_day AS TIMESTAMP)) AS bucket\nFROM implicit_days\n),\ncalendar_time AS (\nSELECT\n  implicit_calendar.bucket AS t\nFROM implicit_calendar\nCROSS JOIN dense_bounds\nWHERE\n  implicit_calendar.bucket >= dense_bounds.range_start\n  AND implicit_calendar.bucket <= dense_bounds.range_end\nGROUP BY\n  implicit_calendar.bucket\n),\nleaf_time_keys AS (\nSELECT\n  leaf_base.t AS t,\n  1 AS source_present\nFROM leaf_base\nWHERE\n  leaf_base.t IS NOT NULL\nGROUP BY\n  leaf_base.t\n),\ndense_time AS (\nSELECT\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END AS t\nFROM calendar_time\nFULL OUTER JOIN leaf_time_keys ON calendar_time.t = leaf_time_keys.t\nGROUP BY\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END\n),\nseries_base AS (\nSELECT\n  dense_time.t AS t,\n  leaf_base.m1 AS m1\nFROM dense_time\nLEFT JOIN leaf_base ON dense_time.t = leaf_base.t\n),\ncoverage_1 AS (\nSELECT\n  MIN(DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))) AS loaded_from,\n  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(jaffle_order.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) END) AS loaded_to\nFROM jaffle_order\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(NULLIF(base.m1, 0), CASE WHEN MAX(base.m1) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1\nFROM series_base AS base\nCROSS JOIN coverage_1\n)\nSELECT\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  LAG(base.m1, 12) OVER (ORDER BY base.t ASC) AS prior_year_orders\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC",
    ),
    "rolling": (
        {
            "select": [
                {
                    "as": "rolling_orders",
                    "expression": {
                        "input": {"measure": "measure.jaffle.order_count"},
                        "kind": "rolling",
                        "window": {"unit": "month", "value": 3},
                    },
                }
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        "WITH leaf_1 AS (\nSELECT\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  COUNT(DISTINCT jaffle_order.order_id) AS m1\nFROM jaffle_order\nGROUP BY\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nleaf_base AS (\nSELECT\n  base.t AS t,\n  base.m1 AS m1\nFROM leaf_1 AS base\n),\ndense_bounds AS (\nSELECT\n  MIN(leaf_base.t) AS range_start,\n  MAX(leaf_base.t) AS range_end\nFROM leaf_base\n),\nimplicit_days AS (\nSELECT\n  CAST(day_series.series_day AS DATE) AS date_day\nFROM dense_bounds\nCROSS JOIN LATERAL GENERATE_SERIES(CAST(dense_bounds.range_start AS DATE), CAST(dense_bounds.range_end AS DATE), INTERVAL (1) DAY) AS day_series(series_day)\n),\nimplicit_calendar AS (\nSELECT\n  implicit_days.date_day AS date_day,\n  DATE_TRUNC('month', CAST(implicit_days.date_day AS TIMESTAMP)) AS bucket\nFROM implicit_days\n),\ncalendar_time AS (\nSELECT\n  implicit_calendar.bucket AS t\nFROM implicit_calendar\nCROSS JOIN dense_bounds\nWHERE\n  implicit_calendar.bucket >= dense_bounds.range_start\n  AND implicit_calendar.bucket <= dense_bounds.range_end\nGROUP BY\n  implicit_calendar.bucket\n),\nleaf_time_keys AS (\nSELECT\n  leaf_base.t AS t,\n  1 AS source_present\nFROM leaf_base\nWHERE\n  leaf_base.t IS NOT NULL\nGROUP BY\n  leaf_base.t\n),\ndense_time AS (\nSELECT\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END AS t\nFROM calendar_time\nFULL OUTER JOIN leaf_time_keys ON calendar_time.t = leaf_time_keys.t\nGROUP BY\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END\n),\nseries_base AS (\nSELECT\n  dense_time.t AS t,\n  leaf_base.m1 AS m1\nFROM dense_time\nLEFT JOIN leaf_base ON dense_time.t = leaf_base.t\n),\ncoverage_1 AS (\nSELECT\n  MIN(DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))) AS loaded_from,\n  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(jaffle_order.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) END) AS loaded_to\nFROM jaffle_order\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(NULLIF(base.m1, 0), CASE WHEN MAX(base.m1) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1\nFROM series_base AS base\nCROSS JOIN coverage_1\n)\nSELECT\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  SUM(base.m1) OVER (ORDER BY base.t ASC ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS rolling_orders\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC",
    ),
    "same_source_multi_measure": (
        {
            "group_by": ["dimension.jaffle_store_name"],
            "limit": 10,
            "select": [
                {"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}},
                {"as": "orders", "expression": {"measure": "measure.jaffle.order_count"}},
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        'WITH leaf_1 AS (\nSELECT\n  jaffle_store.store_name AS g1,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  SUM(jaffle_order.order_total_cents / 100.0) AS m1,\n  COUNT(1) AS m1_rows,\n  COUNT(DISTINCT jaffle_order.order_id) AS m2\nFROM jaffle_order\nLEFT JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id\nGROUP BY\n  jaffle_store.store_name,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nguarded_base AS (\nSELECT\n  base.g1 AS g1,\n  base.t AS t,\n  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1,\n  CASE WHEN MAX(base.m2) OVER () > 0 THEN COALESCE(base.m2, 0) END AS m2\nFROM leaf_1 AS base\n)\nSELECT\n  base.g1 AS "dimension.jaffle_store_name",\n  base.t AS "temporal_role.jaffle_order_time__month",\n  base.m1 AS revenue,\n  base.m2 AS orders\nFROM guarded_base AS base\nORDER BY\n  "temporal_role.jaffle_order_time__month" ASC,\n  "dimension.jaffle_store_name" ASC,\n  revenue ASC NULLS LAST,\n  orders ASC NULLS LAST\nLIMIT 10',
    ),
    "single_measure": (
        {
            "group_by": ["dimension.jaffle_store_name"],
            "limit": 5,
            "select": [{"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        'WITH leaf_1 AS (\nSELECT\n  jaffle_store.store_name AS g1,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  SUM(jaffle_order.order_total_cents / 100.0) AS m1,\n  COUNT(1) AS m1_rows\nFROM jaffle_order\nLEFT JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id\nGROUP BY\n  jaffle_store.store_name,\n  DATE_TRUNC(\'month\', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nguarded_base AS (\nSELECT\n  base.g1 AS g1,\n  base.t AS t,\n  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1\nFROM leaf_1 AS base\n)\nSELECT\n  base.g1 AS "dimension.jaffle_store_name",\n  base.t AS "temporal_role.jaffle_order_time__month",\n  base.m1 AS revenue\nFROM guarded_base AS base\nORDER BY\n  "temporal_role.jaffle_order_time__month" ASC,\n  "dimension.jaffle_store_name" ASC,\n  revenue ASC NULLS LAST\nLIMIT 5',
    ),
    "rolling_average_order": (
        {
            "select": [
                {
                    "as": "aov",
                    "expression": {
                        "kind": "rolling",
                        "input": {"metric": "metric.sales.aov_usd"},
                        "window": {"unit": "month", "value": 3},
                    },
                }
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        "WITH leaf_1 AS (\nSELECT\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  SUM(jaffle_order.order_total_cents / 100.0) AS m1,\n  COUNT(1) AS m1_rows,\n  COUNT(DISTINCT jaffle_order.order_id) AS m2\nFROM jaffle_order\nGROUP BY\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nleaf_base AS (\nSELECT\n  base.t AS t,\n  base.m1 AS m1,\n  base.m2 AS m2,\n  base.m1_rows AS m1_rows\nFROM leaf_1 AS base\n),\ndense_bounds AS (\nSELECT\n  MIN(leaf_base.t) AS range_start,\n  MAX(leaf_base.t) AS range_end\nFROM leaf_base\n),\nimplicit_days AS (\nSELECT\n  CAST(day_series.series_day AS DATE) AS date_day\nFROM dense_bounds\nCROSS JOIN LATERAL GENERATE_SERIES(CAST(dense_bounds.range_start AS DATE), CAST(dense_bounds.range_end AS DATE), INTERVAL (1) DAY) AS day_series(series_day)\n),\nimplicit_calendar AS (\nSELECT\n  implicit_days.date_day AS date_day,\n  DATE_TRUNC('month', CAST(implicit_days.date_day AS TIMESTAMP)) AS bucket\nFROM implicit_days\n),\ncalendar_time AS (\nSELECT\n  implicit_calendar.bucket AS t\nFROM implicit_calendar\nCROSS JOIN dense_bounds\nWHERE\n  implicit_calendar.bucket >= dense_bounds.range_start\n  AND implicit_calendar.bucket <= dense_bounds.range_end\nGROUP BY\n  implicit_calendar.bucket\n),\nleaf_time_keys AS (\nSELECT\n  leaf_base.t AS t,\n  1 AS source_present\nFROM leaf_base\nWHERE\n  leaf_base.t IS NOT NULL\nGROUP BY\n  leaf_base.t\n),\ndense_time AS (\nSELECT\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END AS t\nFROM calendar_time\nFULL OUTER JOIN leaf_time_keys ON calendar_time.t = leaf_time_keys.t\nGROUP BY\n  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END\n),\nseries_base AS (\nSELECT\n  dense_time.t AS t,\n  leaf_base.m1 AS m1,\n  leaf_base.m2 AS m2,\n  leaf_base.m1_rows AS m1_rows\nFROM dense_time\nLEFT JOIN leaf_base ON dense_time.t = leaf_base.t\n),\ncoverage_1 AS (\nSELECT\n  MIN(DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))) AS loaded_from,\n  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(jaffle_order.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(jaffle_order.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) END) AS loaded_to\nFROM jaffle_order\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1,\n  COALESCE(NULLIF(base.m2, 0), CASE WHEN MAX(base.m2) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m2\nFROM series_base AS base\nCROSS JOIN coverage_1\n)\nSELECT\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  SUM(base.m1) OVER (ORDER BY base.t ASC ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) / NULLIF(SUM(base.m2) OVER (ORDER BY base.t ASC ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 0) AS aov\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC",
    ),
    "cumulative_average_order": (
        {
            "select": [
                {
                    "as": "aov",
                    "expression": {
                        "kind": "cumulative",
                        "input": {"metric": "metric.sales.aov_usd"},
                    },
                }
            ],
            "time": {"grain": "month", "temporal_role": "temporal_role.jaffle_order_time"},
        },
        "WITH leaf_1 AS (\nSELECT\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,\n  SUM(jaffle_order.order_total_cents / 100.0) AS m1,\n  COUNT(1) AS m1_rows,\n  COUNT(DISTINCT jaffle_order.order_id) AS m2\nFROM jaffle_order\nGROUP BY\n  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1,\n  CASE WHEN MAX(base.m2) OVER () > 0 THEN COALESCE(base.m2, 0) END AS m2\nFROM leaf_1 AS base\n)\nSELECT\n  base.t AS \"temporal_role.jaffle_order_time__month\",\n  SUM(base.m1) OVER (ORDER BY base.t ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) / NULLIF(SUM(base.m2) OVER (ORDER BY base.t ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) AS aov\nFROM guarded_base AS base\nORDER BY\n  \"temporal_role.jaffle_order_time__month\" ASC",
    ),
}


@pytest.mark.parametrize("case_name", sorted(SNAPSHOT_CASES))
def test_rendered_sql_snapshot_guardrail(package_config_factory, case_name):
    config, _ = package_config_factory("jaffle_shop")
    query, expected_sql = SNAPSHOT_CASES[case_name]

    compiled = compile_query(config, Registry(config), query)

    assert compiled["sql"] == expected_sql


SHOP_SNAPSHOT_CASES = {
    "distribution-sum-preserves-an-explicit-else-zero": 'WITH agent_branch_1__total__entity_values__leaf_1 AS (\nSELECT\n  orders.customer_id AS g1,\n  DATE_TRUNC(\'week\', CAST(orders.ordered_at AS TIMESTAMP)) AS t,\n  SUM(CASE WHEN orders.store_id = \'a\' THEN orders.amount ELSE 0 END) AS m1\nFROM orders\nWHERE\n  orders.ordered_at >= \'2023-11-20\'\n  AND orders.ordered_at < \'2023-11-27\'\nGROUP BY\n  orders.customer_id,\n  DATE_TRUNC(\'week\', CAST(orders.ordered_at AS TIMESTAMP))\n),\nagent_branch_1__total__entity_values AS (\nSELECT\n  base.g1 AS "dimension.shop_customer_id",\n  base.t AS "temporal_role.shop_order_ordered_at__week",\n  base.m1 AS __entity_value\nFROM agent_branch_1__total__entity_values__leaf_1 AS base\n),\nagent_branch_1 AS (\nSELECT\n  agent_branch_1__total__entity_values."temporal_role.shop_order_ordered_at__week" AS "temporal_role.shop_order_ordered_at__week",\n  SUM(agent_branch_1__total__entity_values.__entity_value) AS total\nFROM agent_branch_1__total__entity_values\nGROUP BY\n  agent_branch_1__total__entity_values."temporal_role.shop_order_ordered_at__week"\n),\nagent_projected AS (\nSELECT\n  base."temporal_role.shop_order_ordered_at__week" AS "temporal_role.shop_order_ordered_at__week",\n  base.total AS total\nFROM agent_branch_1 AS base\n)\nSELECT\n  agent_projected."temporal_role.shop_order_ordered_at__week" AS "temporal_role.shop_order_ordered_at__week",\n  agent_projected.total AS total\nFROM agent_projected\nORDER BY\n  "temporal_role.shop_order_ordered_at__week" ASC',
    "window-total-observes-a-conditional-count-outside-the-window": "WITH leaf_1 AS (\nSELECT\n  CASE WHEN (orders.ordered_at IS NULL) THEN 0 ELSE 0 END AS t,\n  COUNT(DISTINCT CASE WHEN orders.store_id = 'a' THEN orders.order_id END) AS m1,\n  COUNT(DISTINCT CASE WHEN orders.store_id = 'b' THEN orders.order_id END) AS m2\nFROM orders\nWHERE\n  orders.ordered_at >= '2023-11-27'\n  AND orders.ordered_at < '2023-12-04'\nGROUP BY\n  CASE WHEN (orders.ordered_at IS NULL) THEN 0 ELSE 0 END\n),\ncoverage_1 AS (\nSELECT\n  MIN(CASE WHEN (orders.ordered_at IS NULL) THEN NULL WHEN FALSE = (orders.ordered_at >= '2023-11-27') THEN -1 WHEN FALSE = (orders.ordered_at < '2023-12-04') THEN 1 ELSE 0 END) AS loaded_from,\n  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(orders.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(orders.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(orders.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN CASE WHEN (orders.ordered_at IS NULL) THEN NULL WHEN FALSE = (orders.ordered_at >= '2023-11-27') THEN -1 WHEN FALSE = (orders.ordered_at < '2023-12-04') THEN 1 ELSE 0 END END) AS loaded_to\nFROM orders\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(NULLIF(base.m1, 0), CASE WHEN (MAX(base.m1) OVER () > 0 OR EXISTS (\nSELECT\n  1 AS seen\nFROM orders\nLIMIT 1\n)) AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1,\n  COALESCE(NULLIF(base.m2, 0), CASE WHEN (MAX(base.m2) OVER () > 0 OR EXISTS (\nSELECT\n  1 AS seen\nFROM orders\nLIMIT 1\n)) AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m2\nFROM leaf_1 AS base\nCROSS JOIN coverage_1\n)\nSELECT\n  base.m1 AS a,\n  base.m2 AS b,\n  base.m1 - base.m2 AS net\nFROM guarded_base AS base",
    "relative-window-total-observes-a-conditional-count-outside-the-window": "WITH leaf_1 AS (\nSELECT\n  CASE WHEN (orders.ordered_at IS NULL) THEN 0 ELSE 0 END AS t,\n  COUNT(DISTINCT CASE WHEN orders.store_id = 'a' THEN orders.order_id END) AS m1,\n  COUNT(DISTINCT CASE WHEN orders.store_id = 'b' THEN orders.order_id END) AS m2\nFROM orders\nWHERE\n  orders.ordered_at >= '2023-11-27'\n  AND orders.ordered_at < '2023-12-04'\nGROUP BY\n  CASE WHEN (orders.ordered_at IS NULL) THEN 0 ELSE 0 END\n),\ncoverage_1 AS (\nSELECT\n  MIN(CASE WHEN (orders.ordered_at IS NULL) THEN NULL WHEN FALSE = (orders.ordered_at >= '2023-11-27') THEN -1 WHEN FALSE = (orders.ordered_at < '2023-12-04') THEN 1 ELSE 0 END) AS loaded_from,\n  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(orders.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(orders.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(orders.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN CASE WHEN (orders.ordered_at IS NULL) THEN NULL WHEN FALSE = (orders.ordered_at >= '2023-11-27') THEN -1 WHEN FALSE = (orders.ordered_at < '2023-12-04') THEN 1 ELSE 0 END END) AS loaded_to\nFROM orders\n),\nguarded_base AS (\nSELECT\n  base.t AS t,\n  COALESCE(NULLIF(base.m1, 0), CASE WHEN (MAX(base.m1) OVER () > 0 OR EXISTS (\nSELECT\n  1 AS seen\nFROM orders\nLIMIT 1\n)) AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1,\n  COALESCE(NULLIF(base.m2, 0), CASE WHEN (MAX(base.m2) OVER () > 0 OR EXISTS (\nSELECT\n  1 AS seen\nFROM orders\nLIMIT 1\n)) AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m2\nFROM leaf_1 AS base\nCROSS JOIN coverage_1\n)\nSELECT\n  base.m1 AS a,\n  base.m2 AS b,\n  base.m1 - base.m2 AS net\nFROM guarded_base AS base",
}


@pytest.mark.parametrize("case_name", SHOP_SNAPSHOT_CASES)
def test_shop_settlement_sql_snapshot(case_name):
    config = load_package_config(str(SHOP))
    query = dict(load_entries())[f"shop/{case_name}"]["query"]
    assert compile_query(config, Registry(config), query)["sql"] == SHOP_SNAPSHOT_CASES[case_name]
