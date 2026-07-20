from __future__ import annotations

import re
from dataclasses import replace

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.temporal import _requires_query_time
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.registry import Registry
from semantic_rails.schema import ConnectionSpec


def _snowflake_config(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="snowflake",
            default_db="",
            seed=replace(config.package.seed, kind="", source="", post_sql=""),
            connection=ConnectionSpec(kind="snowflake_cli", name="semantic_views_trial"),
        ),
    )


def _default_time(recipe):
    role = recipe.temporal_role or (
        recipe.compatible_temporal_roles[0] if recipe.compatible_temporal_roles else ""
    )
    assert role, f"{recipe.id} requires time but has no compatible role"
    # Match _default_time_spec_for_metric in config_validation.py: pick the
    # coarsest window unit found in the metric expression so prior_period
    # / rolling metrics whose offset.unit is month/quarter/year don't blow
    # up at day grain.
    from semantic_rails.config_validation import _metric_min_window_unit

    coarsest = _metric_min_window_unit(recipe.expression)
    grain = coarsest if coarsest in {"month", "quarter", "year"} else "day"
    return {"temporal_role": role, "grain": grain}


def _probe_queries(config):
    for measure in config.measures:
        yield (
            f"measure:{measure.id}",
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": measure.id,
                            "aggregation": measure.default_aggregation,
                        },
                        "as": "probe_value",
                    }
                ],
                "limit": 0,
            },
        )
    for recipe in config.metric_recipes:
        query = {
            "version": 1,
            "select": [{"expression": {"metric": recipe.id}, "as": "probe_value"}],
            "limit": 0,
        }
        if _requires_query_time(recipe.expression, config):
            query["time"] = _default_time(recipe)
        yield f"metric:{recipe.id}", query


EDGE_CASE_QUERIES = {
    "store_month_orders_aov": {
        "version": 1,
        "select": [
            {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            {"expression": {"metric": "metric.sales.aov_usd"}, "as": "aov_usd"},
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        "limit": 5,
    },
    "entity_only_metric_filter": {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
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
    },
    "contextual_metric_filter": {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
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
    },
    "scoped_aggregate_contextual_predicate": {
        "version": 2,
        "select": [
            {
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                    "predicates": [
                        {
                            "measure": "measure.jaffle.order_count",
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
    },
    "daily_scoped_aggregate_month_predicate": {
        "version": 2,
        "select": [
            {
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                    "predicates": [
                        {
                            "measure": "measure.jaffle.order_count",
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
    },
    "conversion": {
        "version": 1,
        "select": [
            {
                "expression": {"metric": "metric.sales.session_to_order_conversion_rate_7d"},
                "as": "conversion_rate",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
    },
    "same_store_conversion": {
        "version": 1,
        "select": [
            {
                "expression": {
                    "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
                },
                "as": "same_store_conversion_rate",
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
        "limit": 10,
    },
    "snapshot_last_value": {
        "version": 1,
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.inventory_on_hand_eop",
                    "aggregation": "last_value",
                },
                "as": "inventory_on_hand_eop",
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
        "limit": 5,
    },
    "percentile": {
        "version": 1,
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.item_revenue_usd",
                    "aggregation": "percentile",
                    "parameters": {"p": 0.95},
                },
                "as": "p95_item_revenue",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        "limit": 5,
    },
    "median": {
        "version": 1,
        "select": [
            {
                "expression": {"metric": "metric.sales.median_item_revenue"},
                "as": "median_item_revenue",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_daily_metric_day", "grain": "day"},
        "limit": 5,
    },
    "time_fill": {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "day",
            "start": "2016-08-25 00:00:00",
            "end": "2016-09-03 00:00:00",
            "fill": True,
        },
        "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
    },
    "rolling": {
        "version": 1,
        "select": [
            {
                "expression": {
                    "kind": "rolling",
                    "input": {"measure": "measure.jaffle.order_count"},
                    "window": {"unit": "day", "value": 3},
                },
                "as": "orders_3d",
            }
        ],
        # Bounded `start` is rejected for windowed kinds (CRITICAL #1) —
        # use only `end` so the window function sees the full lookback.
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "day",
            "end": "2016-09-03 00:00:00",
        },
        "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
    },
    "prior_period_period_to_date": {
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
        # Bounded `start` is rejected for windowed kinds (CRITICAL #1).
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
            "end": "2018-02-01 00:00:00",
        },
        "order_by": [{"field": "temporal_role.jaffle_order_time__month", "direction": "ASC"}],
    },
}


def test_jaffle_shop_compiles_all_probes_and_edge_cases_for_duckdb_and_snowflake():
    duckdb_config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    configs = {"duckdb": duckdb_config, "snowflake": _snowflake_config(duckdb_config)}
    compiled = {warehouse: {} for warehouse in configs}

    for warehouse, config in configs.items():
        registry = Registry(config)
        for name, query in [*_probe_queries(config), *EDGE_CASE_QUERIES.items()]:
            compiled[warehouse][name] = compile_query(config, registry, query)["sql"]

    snowflake_sql = "\n".join(compiled["snowflake"].values())
    duckdb_sql = "\n".join(compiled["duckdb"].values())
    for forbidden in [
        "QUANTILE_CONT(",
        "arg_min(",
        "arg_max(",
        "IS NOT DISTINCT FROM",
        "DATE_DIFF(",
        "generate_series(",
    ]:
        assert forbidden not in snowflake_sql
    assert re.search(r"AS TIMESTAMP\)", snowflake_sql) is None
    for forbidden in ["EQUAL_NULL(", "TIMESTAMP_NTZ"]:
        assert forbidden not in duckdb_sql

    assert "PERCENTILE_CONT(0.95) WITHIN GROUP" in compiled["snowflake"]["percentile"]
    assert "QUANTILE_CONT(" in compiled["duckdb"]["percentile"]
    assert "QUALIFY" in compiled["snowflake"]["snapshot_last_value"]
    assert "QUALIFY" not in compiled["duckdb"]["snapshot_last_value"]
    assert "DATEDIFF(" in compiled["snowflake"]["conversion"]
    assert "DATE_DIFF(" in compiled["duckdb"]["conversion"]
