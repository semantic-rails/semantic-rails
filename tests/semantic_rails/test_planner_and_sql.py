from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import duckdb
import pytest

import semantic_rails.compiler as compiler_module
from semantic_rails.compiler import compile_query
from semantic_rails.db import Database
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    AggregateExpr,
    ArithmeticExpr,
    ColumnRefExpr,
    MetricRecipeRefExpr,
)
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.runtime_parts.assumptions import split_hidden
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MetricConfig,
    PackageConfig,
    PackageMeta,
    RelationshipConfig,
)
from semantic_rails.sql_ast import SqlSelect
from tests.semantic_rails.planner_fixtures import jaffle_runtime

ORDER_TIME = "temporal_role.jaffle_order_time"
SESSION_TIME = "temporal_role.jaffle_session_started_at"
INVENTORY_DAY = "temporal_role.jaffle_inventory_day"
MONTHLY = {"temporal_role": ORDER_TIME, "grain": "month"}
STORE_NAME = "dimension.jaffle_store_name"
PRODUCT_TYPE = "dimension.jaffle_product_type"


def _measure(measure: str, alias: str, aggregation: str = "count_distinct") -> dict[str, Any]:
    expression = {"kind": "measure", "measure": f"measure.jaffle.{measure}"}
    return {"expression": {**expression, "aggregation": aggregation}, "as": alias}


ORDERS = _measure("order_count", "orders")
REVENUE = {"expression": "measure.jaffle.revenue_usd", "as": "revenue"}


runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


@pytest.fixture(scope="module")
def config(runtime: Runtime) -> PackageConfig:
    return runtime.config


def _engine(config: PackageConfig, call: dict) -> dict:
    """A call in the engine's form, read as the front door reads it."""
    return read_call(call, config=config)[0]


def _compiled(config: PackageConfig, call: dict) -> dict[str, Any]:
    return compile_query(config, Registry(config), _engine(config, call))


def _sql(config: PackageConfig, call: dict) -> str:
    return _compiled(config, call)["sql"]


def test_planner_explain_shape_and_sql_rendering(config):
    query = {
        "select": [ORDERS, "metric.sales.aov_usd"],
        "group_by": [STORE_NAME],
        "time": MONTHLY,
        "order_by": [{"field": "orders", "direction": "desc"}],
        "limit": 5,
    }
    compiled = _compiled(config, query)
    plan = compiled["logical_plan"]
    rendered = compiled["sql"]

    assert plan.version == 2
    assert plan.root_entity == "entity.jaffle_order"
    assert plan.selected_paths["entity.jaffle_store"] == ["relationship.orders_store"]
    assert plan.grain_analysis["root_entity"] == "entity.jaffle_order"
    assert plan.fanout_strategy["status"] == "ok"
    assert isinstance(compiled["sql_ast"], SqlSelect)
    assert compiled["sql_ast"].limit == 5
    assert "COUNT(DISTINCT jaffle_order.order_id)" in rendered
    # The answer's column names are always quoted.
    assert '"orders" DESC' in rendered
    performance = compiled["explain"].performance_plan
    assert compiled["explain"].physical_plan["nodes"]
    assert performance["semantic_equivalence"] == "exact_primitives"
    assert performance["risk_level"] == "low"
    assert performance["date_filter_pushdown"][0]["pushed_to_scan"] is True
    assert "alias_resolution" in compiled["explain"].__dict__


@pytest.mark.parametrize(
    ("measure", "alias", "dimension", "sql_present", "sql_absent", "anchor_relation"),
    [
        pytest.param(
            "measure.jaffle.order_count",
            "orders",
            PRODUCT_TYPE,
            [
                "COUNT(DISTINCT jaffle_item.order_id)",
                "FROM jaffle_item",
                "INNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id",
            ],
            ["FROM jaffle_order"],
            "jaffle_item",
            id="order_count_by_product_type",
        ),
        pytest.param(
            "measure.jaffle.customer_count",
            "customers",
            PRODUCT_TYPE,
            [
                "COUNT(DISTINCT jaffle_order.customer_id)",
                "FROM jaffle_order",
                "INNER JOIN jaffle_customer ON jaffle_order.customer_id = jaffle_customer.customer_id",
            ],
            ["FROM jaffle_customer"],
            "jaffle_order",
            id="customer_count_by_product_type",
        ),
        pytest.param(
            "measure.jaffle.customer_count",
            "customers",
            STORE_NAME,
            [
                "COUNT(DISTINCT jaffle_order.customer_id)",
                "FROM jaffle_order",
                "INNER JOIN jaffle_customer ON jaffle_order.customer_id = jaffle_customer.customer_id",
                "JOIN jaffle_store",
            ],
            ["jaffle_storefront_session", "FROM jaffle_customer"],
            "jaffle_order",
            id="customer_count_by_store",
        ),
    ],
)
def test_entity_in_terms_of_uses_physical_anchor(
    runtime, measure, alias, dimension, sql_present, sql_absent, anchor_relation
):
    expression = {"kind": "measure", "measure": measure, "aggregation": "count_distinct"}
    report = runtime.validate(
        {"select": [{"expression": expression, "as": alias}], "group_by": [dimension]}
    )
    assert report["ok"] is True
    rendered = report["explain"]["rendered_sql"]
    assert all(fragment in rendered for fragment in sql_present), rendered
    assert not any(fragment in rendered for fragment in sql_absent), rendered
    assert report["logical_plan"]["rewrite_steps"][0]["kind"] == "entity_in_terms_of"
    scans = report["explain"]["performance_plan"]["estimated_scan_relations"]
    assert scans[0]["relation"] == anchor_relation


def test_entity_in_terms_of_order_count_by_product_type_keeps_order_time_lookup(runtime):
    report = runtime.validate({"select": [ORDERS], "group_by": [PRODUCT_TYPE], "time": MONTHLY})
    assert report["ok"] is True
    rendered = report["explain"]["rendered_sql"]
    assert "FROM jaffle_item" in rendered
    assert "INNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id" in rendered
    assert "DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))" in rendered
    [join, *_] = [
        node for node in report["explain"]["physical_plan"]["nodes"] if node["kind"] == "Join"
    ]
    assert join["details"]["rewrite_reason"] == "entity_in_terms_of"
    assert join["details"]["physical_anchor_entity"] == "entity.jaffle_item"
    assert join["details"]["omitted_semantic_root_entity"] == "entity.jaffle_order"
    assert join["details"]["linked_dimensions"] == [
        {
            "dimension_id": PRODUCT_TYPE,
            "target_entity": "entity.jaffle_product",
            "physical_anchor_entity": "entity.jaffle_item",
            "rewrite_reason": "entity_in_terms_of",
        }
    ]


@pytest.mark.parametrize(
    ("call", "present", "absent"),
    [
        pytest.param(
            {
                "select": [ORDERS],
                "group_by": [PRODUCT_TYPE],
                "where": [{"field": STORE_NAME, "op": "is", "value": "Brooklyn"}],
            },
            # An item whose order has no record counts no order; the store lookup past the
            # order keeps its rows, and the filter on the store drops those it finds no match for.
            [
                "FROM jaffle_item",
                "INNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id",
                "LEFT JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id",
                "jaffle_store.store_name = 'Brooklyn'",
            ],
            [],
            id="root-lookup-filter",
        ),
        pytest.param(
            {
                "select": [
                    {"expression": "metric.sales.rolling_7d_revenue_direct", "as": "rev_7d"}
                ],
                "group_by": [PRODUCT_TYPE],
                "time": {"temporal_role": ORDER_TIME, "grain": "day"},
            },
            ["jaffle_item.item_revenue_cents / 100.0", "JOIN jaffle_product"],
            [],
            id="product-grain-rolling-revenue",
        ),
        pytest.param(
            {
                "select": [
                    {
                        "expression": "metric.sales.session_to_order_conversion_rate_7d",
                        "as": "conversion_rate",
                    }
                ],
                "time": {"temporal_role": SESSION_TIME, "grain": "day"},
            },
            [
                "ROW_NUMBER()",
                "< DATE_ADD(CAST(base_events.__base_event_time AS TIMESTAMP), INTERVAL (7) DAY)",
            ],
            [],
            id="conversion-event-pair",
        ),
        pytest.param(
            {
                "select": [
                    {
                        "expression": "metric.sales.session_to_order_conversion_rate_7d_same_store",
                        "as": "rate",
                    }
                ],
                "time": {"temporal_role": SESSION_TIME, "grain": "day"},
            },
            [],
            [],
            id="same-store-conversion-on-its-anchor",
        ),
    ],
)
def test_a_valid_call_reads_the_rows_its_join_path_names(runtime, call, present, absent):
    report = runtime.validate(call)
    assert report["ok"] is True, report
    rendered = report["explain"]["rendered_sql"]
    assert all(fragment in rendered for fragment in present), rendered
    assert not any(fragment in rendered for fragment in absent), rendered
    result = runtime.query(call)
    assert result["status"] == "ok"
    assert result["row_count"] >= 1


def test_relative_time_range_expansion(runtime):
    time = {**MONTHLY, "last": {"unit": "month", "value": 3}}
    report = runtime.validate({"select": [REVENUE], "time": time}, now="2026-05-04")
    assert report["ok"] is True
    # The call keeps its relative spelling; the answer's window names the dates it reads.
    assert report["normalized_query"]["time"]["last"] == {"unit": "month", "value": 3}
    assert (report["window"]["start"], report["window"]["end"]) == ("2026-02-01", "2026-05-01")


def _has(details: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Each expected detail: equal, or a member of a list the details carry."""
    return all(
        details.get(key) == value
        or (isinstance(details.get(key), list) and not isinstance(value, list)
            and value in details[key])
        for key, value in expected.items()
    )  # fmt: skip


SCOPED_COUNT = {
    "kind": "scoped_aggregate",
    "measure": "measure.jaffle.order_count",
    "aggregation": "count_distinct",
}


def _entity_filter_query(*, entity: str, value: int = 0, where: tuple = (), **extra) -> dict:
    """A call that counts orders of the entities whose order count, over all their rows, is
    above ``value``: a metric predicate built from an entity filter."""
    having = [{"field": "measure.jaffle.order_count", "op": "gt", "value": value}]
    filter_ = {"entity": entity, "having": having, "over": "all_time"}
    return {"select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
            "where": [filter_, *where], **extra}  # fmt: skip


SAME_STORE_ON_ORDER_TIME = {
    "select": [
        {"expression": "metric.sales.session_to_order_conversion_rate_7d_same_store", "as": "rate"}
    ],
    "time": {"temporal_role": ORDER_TIME, "grain": "day"},
}
REFUSED = [
    pytest.param(
        # A role named outside the typed prefixes looks for matches in every namespace.
        {"select": [ORDERS], "time": {"temporal_role": "t.does_not_exist", "grain": "month"}},
        "INVALID_TEMPORAL_ROLE",
        {},
        id="temporal-role-unknown",
    ),
    pytest.param(
        {"select": [ORDERS], "time": {"temporal_role": "temporal_role.jaffle_order_tim", "grain": "month"}},
        "INVALID_TEMPORAL_ROLE", {"closest_matches": ORDER_TIME}, id="temporal-role-typo",
    ),
    pytest.param(
        {"select": [{"expression": "metric.sales.reveneu_usd", "as": "revenue"}]},
        "OBJECT_NOT_FOUND", {"closest_matches": "metric.sales.revenue_ytd"}, id="metric-typo",
    ),
    pytest.param(
        {"select": [_measure("revenue_usd", "rev", "sum")], "group_by": ["dimension.jaffle_store_nam"]},
        "OBJECT_NOT_FOUND", {"closest_matches": STORE_NAME}, id="dimension-typo",
    ),
    pytest.param(
        {"select": [_measure("revneue_usd", "rev", "sum")]},
        "OBJECT_NOT_FOUND", {"closest_matches": "measure.jaffle.revenue_usd"}, id="measure-typo",
    ),
    pytest.param(
        {"select": [{"expression": "metric.sales.inventory_on_hand_eop", "as": "inventory"}],
         "group_by": ["dimension.jaffle_customer_type"]},
        "PATH_NOT_FOUND", {}, id="inventory-by-customer-type",
    ),
    pytest.param(
        {"select": [REVENUE], "where": [{"expression": {"kind": "literal", "value": True}, "op": "is", "value": True}]},
        "INVALID_QUERY", {"path": "where[0].expression", "supported_keys": ["field", "op", "value"]},
        id="where-expression",
    ),
    pytest.param(
        {"select": [{"expression": "measure.jaffle.order_count", "as": "dup"},
                    {"expression": "measure.jaffle.revenue_usd", "as": "dup"}], "time": MONTHLY},
        "DUPLICATE_OUTPUT_ALIAS", {}, id="duplicate-select-alias",
    ),
    pytest.param(
        {"select": [{"expression": "measure.jaffle.order_count", "as": STORE_NAME}],
         "group_by": [STORE_NAME], "time": MONTHLY},
        "DUPLICATE_OUTPUT_ALIAS", {}, id="alias-of-a-grouping",
    ),
    pytest.param(
        {"select": [{"expression": "measure.jaffle.order_count", "as": "period"}], "time": MONTHLY},
        "DUPLICATE_OUTPUT_ALIAS", {}, id="alias-of-the-period",
    ),
    pytest.param({"time": {"grain": "month"}}, "INVALID_QUERY", {}, id="time-only-without-role"),
    pytest.param(
        {"select": [_measure("item_revenue_usd", "item_revenue_usd", "sum")],
         "time": {"temporal_role": "temporal_role.jaffle_store_opened_at", "grain": "month"}},
        "INCOMPATIBLE_TEMPORAL_ROLE", {}, id="incompatible-role-measure-kind",
    ),
    pytest.param(
        {"select": ["metric.sales.rolling_28d_revenue"], "time": MONTHLY},
        "INCOMPATIBLE_TEMPORAL_ROLE", {}, id="incompatible-role-metric",
    ),
    pytest.param(
        {"select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
         "time": {"temporal_role": "temporal_role.jaffle_store_opened_at", "grain": "month"}},
        "INCOMPATIBLE_TEMPORAL_ROLE", {}, id="incompatible-role-measure-id",
    ),
    pytest.param(
        {"select": ["metric.sales.cumulative_revenue"],
         "time": {**MONTHLY, "start": "2017-01-01 00:00:00", "end": "2017-04-01 00:00:00"}},
        "CUMULATIVE_TIME_FILTER_UNSUPPORTED", {}, id="cumulative-with-a-start",
    ),
    pytest.param(
        {"select": ["metric.sales.cumulative_revenue"]}, "INVALID_QUERY", {},
        id="cumulative-without-time",
    ),
    pytest.param(
        {"select": [{"as": "orders_7d", "expression": {
            "kind": "rolling", "input": "measure.jaffle.order_count",
            "window": {"unit": "day", "value": 7}}}], "time": MONTHLY},
        "REWRITE_NOT_SUPPORTED", {}, id="rolling-window-finer-than-the-grain",
    ),
    pytest.param(
        {"select": [{"as": "orders_prev_7d", "expression": {
            "kind": "prior_period", "input": "measure.jaffle.order_count",
            "offset": {"unit": "day", "value": 7}}}], "time": MONTHLY},
        "REWRITE_NOT_SUPPORTED", {}, id="prior-period-finer-than-the-grain",
    ),
    pytest.param(
        {"select": [{"as": "repeat_customers_only", "expression": {
            "kind": "metric_predicate", "entity": "entity.jaffle_customer",
            "input": "measure.jaffle.lifetime_order_count", "op": "gt", "value": 1,
            "over": "all_time"}}]},
        "INVALID_METRIC_PREDICATE", {}, id="predicate-is-not-selectable",
    ),
    pytest.param(
        {"select": [{"as": "orders", "expression": {**SCOPED_COUNT, "predicates": [
            {"measure": "measure.jaffle.order_count", "entity": "entity.jaffle_customer",
             "op": ">", "value": 10, "time_grain": "month"}]}}],
         "time": {"temporal_role": ORDER_TIME, "grain": "day"}},
        "INVALID_QUERY", {"path": "select[0].expression.predicates[0].time_grain"},
        id="predicate-grain-in-a-call",
    ),
    pytest.param(
        {"select": [{"as": "bad_conversion", "expression": {
            "kind": "conversion", "entity": "entity.jaffle_customer",
            "window": {"unit": "day", "value": 7}, "matching_mode": "first_converted_after_base",
            "base": {"kind": "aggregate", "measure": "measure.jaffle.revenue_usd"},
            "converted": {"kind": "aggregate", "measure": "measure.jaffle.revenue_usd"}}}],
         "time": {"temporal_role": ORDER_TIME, "grain": "day"}},
        # Revenue is a flow, not an event count: no conversion reads it.
        "CONVERSION_NOT_SUPPORTED", {}, id="conversion-of-a-flow",
    ),
    pytest.param(
        {"select": [{"as": "rate", "expression": {
            "kind": "conversion", "entity": "entity.jaffle_customer",
            "window": {"unit": "day", "value": 28}, "matching_mode": "first_converted_after_base",
            "base": {"kind": "aggregate", "measure": "measure.jaffle.session_starts"},
            "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"}}}],
         "group_by": [PRODUCT_TYPE], "time": {"temporal_role": SESSION_TIME, "grain": "month"}},
        "CONVERSION_NOT_SUPPORTED",
        {"required_binding": {"side": "converted", "denominator": "all_base_events"}},
        id="conversion-by-converted-side-needs-a-denominator",
    ),
    pytest.param(
        # The conversion leaf reads its session anchor, never the order time the call names.
        SAME_STORE_ON_ORDER_TIME, "INVALID_TEMPORAL_BINDING",
        {"anchor_temporal_role": SESSION_TIME, "allowed_temporal_roles": SESSION_TIME},
        id="conversion-off-its-anchor",
    ),
    pytest.param(
        {"select": [{"as": "orders", "expression": {**SCOPED_COUNT, "predicates": [
            {"entity": "entity.jaffle_customer", "input": {"kind": "literal", "value": 1},
             "op": "gt", "value": 0, "over": "all_time"}]}}]},
        "PREDICATE_NOT_SUPPORTED", {}, id="predicate-over-a-literal",
    ),
    pytest.param(
        # The query's own path to the predicate entity crosses the time-valid hop first.
        _entity_filter_query(entity="entity.jaffle_customer_history"),
        "FANOUT_UNSAFE", {}, id="temporal-path-without-query-time",
    ),
    pytest.param(
        # An all-time predicate has no time of its own to pick the history slice by.
        _entity_filter_query(entity="entity.jaffle_customer_history", time=MONTHLY),
        "PREDICATE_SCOPE_UNSAFE", {}, id="temporal-path-with-query-time",
    ),
    pytest.param(
        _entity_filter_query(
            entity="entity.jaffle_customer",
            where=({"field": "dimension.jaffle_customer_history_segment", "op": "is",
                    "value": "high_value"},),
        ),
        "FANOUT_UNSAFE", {}, id="filter-through-a-temporal-path",
    ),
]  # fmt: skip


@pytest.mark.parametrize(("call", "code", "details"), REFUSED)
def test_validate_refuses_with_a_code_and_its_details(runtime, call, code, details):
    report = runtime.validate(call)
    assert report["ok"] is False
    [error, *_] = report["errors"]
    assert error["code"] == code, report["errors"]
    assert _has(error.get("details", {}), details), error


def test_a_string_last_window_is_refused_with_a_hint_that_validates(runtime):
    call = {"select": [REVENUE], "time": {**MONTHLY, "last": "90 days"}}
    [error] = runtime.validate(call)["errors"]
    assert (error["code"], error["details"]["path"]) == ("INVALID_QUERY", "time.last")
    [hint] = error["recovery_hints"]
    shape = json.loads(
        hint["message"][hint["message"].index("{") : hint["message"].rindex("}") + 1]
    )
    assert runtime.validate({**call, "time": {**MONTHLY, "last": shape}})["ok"] is True


@pytest.mark.parametrize(
    ("call", "code"),
    [
        ({"select": [_measure("revenue_usd", "rev", "sum")], "group_by": ["dimension.jaffle_store_nam"]},
         "OBJECT_NOT_FOUND"),
        (SAME_STORE_ON_ORDER_TIME, "INVALID_TEMPORAL_BINDING"),
    ],
)  # fmt: skip
def test_compile_raises_the_validate_refusal(runtime, call, code):
    with pytest.raises(SemanticLayerError) as raised:
        runtime.compile(call)
    assert raised.value.code == code
    if code == "OBJECT_NOT_FOUND":
        assert STORE_NAME in raised.value.details["closest_matches"]
    else:
        hints = recovery_hints_for_error(code, raised.value.details)
        assert "filter_on_conversion_anchor" in {hint.get("kind") for hint in hints}


def test_object_not_found_close_matches_on_segment(runtime):
    canonical = runtime.config.segments[0].id
    report = runtime.segment_validate(canonical[:-1])
    assert report["ok"] is False
    [error] = report["errors"]
    assert error["code"] == "OBJECT_NOT_FOUND"
    assert error["details"]["closest_matches"]


def test_group_by_only_distinct_values_compile_without_measures(config, runtime):
    compiled = compile_query(
        config,
        Registry(config),
        {
            "group_by": [STORE_NAME],
            "order_by": [{"field": STORE_NAME, "direction": "ASC"}],
            "limit": 5,
        },
    )
    assert compiled["logical_plan"].root_entity == "entity.jaffle_store"
    assert compiled["logical_plan"].bound_measures == []
    assert compiled["logical_plan"].measure_plans == []
    assert "FROM jaffle_store" in compiled["sql"]
    assert "g1 ASC" in compiled["sql"]
    order = [{"field": STORE_NAME, "direction": "asc"}]
    rows = runtime.query({"group_by": [STORE_NAME], "order_by": order, "limit": 5})["rows"]
    assert rows and list(rows[0]) == [STORE_NAME]


def test_time_only_distinct_values_compile_with_explicit_temporal_role(config, runtime):
    compiled = compile_query(
        config,
        Registry(config),
        {"time": MONTHLY, "order_by": [{"field": "time", "direction": "ASC"}], "limit": 3},
    )
    assert compiled["logical_plan"].root_entity == "entity.jaffle_order"
    assert compiled["logical_plan"].bound_measures == []
    assert "DATE_TRUNC('month'" in compiled["sql"]
    assert "ORDER BY\n  t ASC" in compiled["sql"]
    order = [{"field": "period", "direction": "asc"}]
    rows = runtime.query({"time": MONTHLY, "order_by": order, "limit": 3})["rows"]
    assert rows and "period" in rows[0]


def test_mixed_grain_measures_are_rewritten_into_classified_leaf_aggregates(config, runtime):
    query = {
        "select": [ORDERS, _measure("item_count", "items")],
        "group_by": [STORE_NAME],
        "time": MONTHLY,
    }
    compiled = compile_query(config, Registry(config), query)
    assert compiled["logical_plan"].fanout_strategy["status"] == "rewritten"
    assert any(
        step.measure_id == "measure.jaffle.item_count"
        for step in compiled["logical_plan"].rewrite_steps
    )
    assert "FULL OUTER JOIN leaf_2 AS right_side" in compiled["sql"]
    warnings = runtime.validate(query).get("warnings") or []
    rewrite = next(warning for warning in warnings if warning["code"] == "REWRITE_APPLIED")
    assert rewrite["details"]["rewrite_kind"] == "leaf_preaggregate_join"
    assert rewrite["details"]["rewrite_classification"] == "safe_independent_leaf_alignment"


TWO_FROM_ORDERS = {
    "select": [REVENUE, {"expression": "measure.jaffle.order_count", "as": "orders"}],
    "group_by": [STORE_NAME],
    "time": MONTHLY,
}


def test_same_fact_measures_fold_into_one_leaf_scan(config):
    compiled = _compiled(config, {**TWO_FROM_ORDERS, "limit": 10})
    rendered = compiled["sql"]
    assert "FULL OUTER JOIN" not in rendered
    assert "leaf_2 AS (" not in rendered
    assert rendered.count("FROM jaffle_order") == 1
    assert "leaf_base AS (" not in rendered
    assert "projected AS (" not in rendered
    performance = compiled["explain"].performance_plan
    assert performance["raw_fact_count"] == 1
    assert len(performance["estimated_scan_relations"][0]["measures"]) == 2
    assert performance["full_outer_alignments"] == 0
    assert any(
        row["kind"] == "same_source_measure_folding"
        for row in compiled["physical_plan"].optimizations
    )


def test_alias_tiering_uses_short_internal_columns_and_preserves_final_names(config):
    leaf_sql, final_sql = _sql(config, TWO_FROM_ORDERS).split(")\nSELECT", 1)
    for alias in (" AS g1", " AS t", " AS m1", " AS m2"):
        assert alias in leaf_sql
    assert f'"{STORE_NAME}"' not in leaf_sql
    assert ' AS "period"' not in leaf_sql
    for column in (f'base.g1 AS "{STORE_NAME}"', 'base.t AS "period"', 'base.m1 AS "revenue"'):
        assert column in final_sql
    assert 'base.m2 AS "orders"' in final_sql


def test_bounded_time_filters_are_pushed_into_source_scan(config):
    time = {**MONTHLY, "start": "2017-01-01", "end": "2017-04-01"}
    compiled = _compiled(config, {"select": [REVENUE], "time": time})
    rendered = compiled["sql"]
    performance = compiled["explain"].performance_plan
    source_start = rendered.index("FROM jaffle_order")
    source_scan = rendered[source_start : rendered.index("GROUP BY", source_start)]
    assert performance["risk_level"] == "low"
    assert performance["date_filter_pushdown"][0]["has_start"] is True
    assert performance["date_filter_pushdown"][0]["has_end"] is True
    assert "jaffle_order.ordered_at >= '2017-01-01'" in source_scan
    assert "jaffle_order.ordered_at < '2017-04-01'" in source_scan


def test_multi_fact_metrics_align_on_common_metric_time(config):
    delivered = {"expression": "measure.jaffle.delivered_revenue_usd", "as": "delivered_revenue"}
    query = {
        "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}, delivered],
        "time": MONTHLY,
    }
    compiled = _compiled(config, query)
    rendered = compiled["sql"]
    assert compiled["logical_plan"].fanout_strategy["status"] == "rewritten"
    assert "FULL OUTER JOIN leaf_2 AS right_side" in rendered
    assert "DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t" in rendered
    assert (
        "DATE_TRUNC('month', CAST(jaffle_order_lifecycle.delivered_at AS TIMESTAMP)) AS t"
        in rendered
    )


def _primitive_channel_config() -> PackageConfig:
    def entity(name: str, table: str, key: str) -> EntityConfig:
        return EntityConfig(id=f"entity.{name}", table=table, key_columns=[key])

    def to_account(source: str, target: str = "account", column: str = "account_id"):
        return RelationshipConfig(
            id=f"relationship.{source}_{target}",
            source_entity=f"entity.{source}" + ("_fact" if target == "account" else ""),
            target_entity=f"entity.{'parent_account' if target == 'parent' else 'account'}",
            source_column=column,
            target_column="account_id",
            cardinality="N:1",
            safety="safe",
        )

    channels = ("email", "sms", "push")
    measures = [
        MeasureConfig(
            id=f"measure.{channel}_count",
            entity=f"entity.{channel}_fact",
            row_grain=[f"{channel}_id"],
            expr=ColumnRefExpr(f"{channel}_count"),
            default_aggregation="sum",
            allowed_aggregations=["sum"],
        )
        for channel in channels
    ]
    recipes = [
        MetricConfig(
            id=f"metric.{channel}_count",
            kind="aggregate",
            expression=AggregateExpr(f"measure.{channel}_count", "sum"),
        )
        for channel in channels
    ]
    total = ArithmeticExpr(
        "add",
        ArithmeticExpr(
            "add",
            MetricRecipeRefExpr("metric.email_count"),
            MetricRecipeRefExpr("metric.sms_count"),
        ),
        MetricRecipeRefExpr("metric.push_count"),
    )
    return PackageConfig(
        version=3,
        package=PackageMeta(
            package_id="primitive_channels",
            name="Primitive channels",
            description="",
            warehouse="duckdb",
        ),
        entities=[
            *(
                entity(f"{channel}_fact", f"{channel}_fact", f"{channel}_id")
                for channel in channels
            ),
            entity("account", "account_dim", "account_id"),
            entity("parent_account", "parent_account_dim", "account_id"),
        ],
        dimensions=[
            DimensionConfig(
                id="dimension.parent_account_geo",
                entity="entity.parent_account",
                column="account_geo",
                data_type="string",
            ),
        ],
        temporal_roles=[],
        relationships=[
            *(to_account(channel) for channel in channels),
            to_account("account", "parent", "parent_account_id"),
        ],
        value_domains=[],
        measures=measures,
        metric_recipes=[
            *recipes,
            MetricConfig(id="metric.total_messages", kind="derived", expression=total),
        ],
    )


def test_primitive_only_parent_geo_plan_preaggregates_channel_facts_before_dimension_joins():
    config = _primitive_channel_config()
    query = {"select": ["metric.total_messages"], "group_by": ["dimension.parent_account_geo"]}
    compiled = _compiled(config, query)
    rendered = compiled["sql"]
    performance = compiled["explain"].performance_plan
    assert performance["semantic_equivalence"] == "exact_primitives"
    assert performance["raw_fact_count"] == 3
    assert performance["full_outer_alignments"] == 2
    assert performance["risk_level"] == "high"
    assert performance["execution_recommendation"] == "needs_acceleration_layer"
    assert "primitive_exact_but_expensive" in performance["notes"]
    assert performance["joins_before_aggregate"] == []
    assert len(performance["joins_after_aggregate"]) == 3
    assert all(
        row["strategy"] == "source_preaggregate" for row in performance["joins_after_aggregate"]
    )
    for leaf in (1, 2, 3):
        assert f"leaf_{leaf}_source_rollup AS (" in rendered
    for relation in ("email_fact", "sms_fact", "push_fact"):
        source_start = rendered.index(f"FROM {relation}")
        assert "JOIN" not in rendered[source_start : rendered.index("GROUP BY", source_start)]


def test_duckdb_synthetic_parent_geo_total_messages_matches_oracle():
    config = _primitive_channel_config()
    query = {
        "select": ["metric.total_messages"],
        "group_by": ["dimension.parent_account_geo"],
        "order_by": [{"field": "dimension.parent_account_geo", "direction": "asc"}],
    }
    compiled = _compiled(config, query)
    db = Database.connect(":memory:")
    try:
        db.execute_script(
            """
            CREATE TABLE email_fact(email_id INTEGER, account_id TEXT, email_count INTEGER);
            CREATE TABLE sms_fact(sms_id INTEGER, account_id TEXT, sms_count INTEGER);
            CREATE TABLE push_fact(push_id INTEGER, account_id TEXT, push_count INTEGER);
            CREATE TABLE account_dim(account_id TEXT, parent_account_id TEXT);
            CREATE TABLE parent_account_dim(account_id TEXT, account_geo TEXT);

            INSERT INTO email_fact VALUES (1, 'a1', 10), (2, 'a1', 5), (3, 'a2', 7), (4, 'a3', 2);
            INSERT INTO sms_fact VALUES (1, 'a1', 3), (2, 'a2', 11), (3, 'a4', 100);
            INSERT INTO push_fact VALUES (1, 'a2', 4), (2, 'a3', 8);
            INSERT INTO account_dim VALUES ('a1', 'p1'), ('a2', 'p1'), ('a3', 'p2'), ('a4', 'p3');
            INSERT INTO parent_account_dim VALUES ('p1', 'US'), ('p2', 'EU'), ('p3', 'US');
            """
        )
        # The answer's rows, without the columns the runtime reads and strips.
        actual, _ = split_hidden(db.query(compiled["sql"]), compiled)
        oracle = db.query(
            """
            WITH channel_rows AS (
              SELECT p.account_geo, SUM(e.email_count) AS messages
              FROM email_fact e
              JOIN account_dim a ON e.account_id = a.account_id
              JOIN parent_account_dim p ON a.parent_account_id = p.account_id
              GROUP BY p.account_geo
              UNION ALL
              SELECT p.account_geo, SUM(s.sms_count) AS messages
              FROM sms_fact s
              JOIN account_dim a ON s.account_id = a.account_id
              JOIN parent_account_dim p ON a.parent_account_id = p.account_id
              GROUP BY p.account_geo
              UNION ALL
              SELECT p.account_geo, SUM(u.push_count) AS messages
              FROM push_fact u
              JOIN account_dim a ON u.account_id = a.account_id
              JOIN parent_account_dim p ON a.parent_account_id = p.account_id
              GROUP BY p.account_geo
            )
            SELECT account_geo AS "dimension.parent_account_geo", SUM(messages) AS total_messages
            FROM channel_rows
            GROUP BY account_geo
            ORDER BY account_geo
            """
        )
    finally:
        db.close()

    assert actual == oracle


def test_runtime_binary_expression_can_compose_mixed_clock_metric_primitives(config):
    left = {"kind": "aggregate", "measure": "measure.jaffle.order_count"}
    right = {"kind": "aggregate", "measure": "measure.jaffle.delivered_revenue_usd"}
    expression = {"kind": "arithmetic", "op": "plus", "left": left, "right": right}
    rendered = compile_query(
        config,
        Registry(config),
        {"select": [{"expression": expression, "as": "orders_plus_revenue"}], "time": MONTHLY},
    )["sql"]
    assert "FULL OUTER JOIN leaf_2 AS right_side" in rendered
    # Top-level binary in SELECT list renders without redundant outer parens.
    assert "base.m1 + base.m2" in rendered


# Start-of-period inventory: an end-of-period balance closes on a day, and its share filtered
# by entity sets is refused (test_stock_closing_day_paths).
INVENTORY = {
    "kind": "scoped_aggregate",
    "measure": "measure.jaffle.inventory_on_hand_sop",
    "aggregation": "sum",
}


def _inventory_share(predicate: dict[str, Any], **parts: Any) -> dict[str, Any]:
    numerator = {
        **INVENTORY,
        "predicates": [
            {"measure": "measure.jaffle.order_count", "entity": "entity.jaffle_store", **predicate}
        ],
    }
    ratio = {"kind": "ratio", "numerator": numerator, "denominator": INVENTORY}
    return {"select": [{"as": "share", "expression": ratio}],
            "time": {"temporal_role": INVENTORY_DAY, "grain": "month"}, **parts}  # fmt: skip


ORDERING_STORES = {"op": "gt", "value": 0, "over": "period"}
JANUARY_BY_STORE = {
    "group_by": [STORE_NAME],
    "time": {
        "temporal_role": INVENTORY_DAY,
        "grain": "month",
        "start": "2024-01-01",
        "end": "2024-02-01",
    },
}


def test_scoped_ratio_uses_cross_domain_metric_predicate_sets(config):
    compiled = _compiled(config, _inventory_share(ORDERING_STORES))
    rendered = compiled["sql"]
    assert "predicate_jaffle_store_set_1 AS (" in rendered
    assert 'base.t AS "period"' in rendered or 'anchor.t AS "period"' in rendered
    assert "LEFT JOIN predicate_jaffle_store_set_1" in rendered
    assert "FULL OUTER JOIN" not in rendered
    assert rendered.count("FROM jaffle_store_inventory_snapshot") == 1
    assert any(
        row["kind"] == "anchored_entity_set" for row in compiled["physical_plan"].optimizations
    )
    nodes = compiled["logical_plan"].semantic_dag.nodes
    assert {"PredicateSet", "ScopedSemiJoin"} <= {row.kind for row in nodes}
    assert any(
        row.kind == "PostAggregateCalc" and row.details.get("op") == "ratio" for row in nodes
    )


def test_anchored_ratio_delays_dimension_joins_and_prunes_predicate_context(config):
    compiled = _compiled(config, _inventory_share(ORDERING_STORES, **JANUARY_BY_STORE))
    rendered = compiled["sql"]
    predicate_section = rendered.split("predicate_jaffle_store_set_1 AS (", 1)[0]
    assert "jaffle_store.store_name" not in predicate_section
    assert "LEFT JOIN jaffle_store ON" in rendered
    assert "LEFT JOIN predicate_jaffle_store_set_1" in rendered
    assert "FULL OUTER JOIN" not in rendered
    assert compiled["explain"].performance_plan["full_outer_alignments"] == 0


def test_anchored_ratio_uses_snowflake_qualify_for_snapshot_selection(config):
    snowflake = replace(config, package=replace(config.package, warehouse="snowflake"))
    compiled = _compiled(snowflake, _inventory_share(ORDERING_STORES, **JANUARY_BY_STORE))
    rendered = compiled["sql"]
    assert "QUALIFY" in rendered
    # RANK: rows tied on a series' snapshot are all read, as the join-back reads them.
    assert "RANK() OVER" in rendered
    assert "latest_jaffle_store_inventory_snapshot_snapshot_complete" not in rendered
    assert any(
        row["kind"] == "snapshot_row_selection" and row["strategy"] == "qualify_row_number"
        for row in compiled["physical_plan"].optimizations
    )


def test_anchored_ratio_refuses_a_threshold_that_zero_satisfies(config):
    idle = {"op": "=", "value": 0, "time_alignment": "same_query_period"}
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), _inventory_share(idle))
    assert raised.value.code == "INVALID_METRIC_PREDICATE"
    assert raised.value.details["entity"] == "entity.jaffle_store"


def test_distribution_rolls_snapshot_to_entity_before_percentile(config):
    entity_value = {
        "kind": "entity_value",
        "entity": "entity.jaffle_store",
        "input": {
            "kind": "measure",
            "measure": "measure.jaffle.inventory_on_hand_eop",
            "aggregation": "sum",
        },
        "where": [{"kind": "value_filter", "op": "gt", "value": 0}],
    }
    expression = {"kind": "distribution", "function": "percentile", "p": 0.8, "over": entity_value}
    query = {
        "select": [{"as": "p80_inventory", "expression": expression}],
        "time": {"temporal_role": INVENTORY_DAY, "grain": "month"},
    }
    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]
    assert "p80_inventory__entity_values" in rendered
    assert 'base.g1 AS "dimension.jaffle_store_id"' in rendered
    assert "__entity_value > 0" in rendered
    assert "QUANTILE_CONT(" in rendered
    kinds = {row.kind for row in compiled["logical_plan"].semantic_dag.nodes}
    assert {"EntityRollup", "DistributionAggregate"} <= kinds


def test_metric_time_alignment_does_not_mask_single_leaf_temporal_errors(config):
    both = [ORDER_TIME, "temporal_role.jaffle_lifecycle_delivered_at"]
    measures = [
        replace(measure, compatible_temporal_roles=both)
        if measure.id == "measure.jaffle.item_revenue_usd"
        else measure
        for measure in config.measures
    ]
    widened = replace(config, measures=measures)
    query = {
        "select": [
            {
                "expression": {"measure": "measure.jaffle.item_revenue_usd", "aggregation": "sum"},
                "as": "item_revenue_usd",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_store_opened_at", "grain": "month"},
    }
    with pytest.raises(SemanticLayerError) as exc:
        compile_query(widened, Registry(widened), query)
    assert exc.value.code == "INCOMPATIBLE_TEMPORAL_ROLE"


def test_semi_additive_snapshots_choose_period_row_before_summing(config):
    query = {
        "select": ["metric.sales.inventory_on_hand_eop"],
        "group_by": [STORE_NAME],
        "time": {"temporal_role": INVENTORY_DAY, "grain": "month"},
    }
    rendered = _sql(config, query)
    assert "snapshot_complete AS (" in rendered
    assert "MAX(leaf_1__snapshot_base.__snapshot_order) AS __snapshot_order" in rendered
    # The sum reads the chosen snapshots only.
    assert "SUM(leaf_1__snapshot_rows.__snapshot_value) AS m1" in rendered
    assert "arg_max" not in rendered.lower()


def test_advanced_time_sql_supports_calendar_dense_fill_and_period_rewrites(config):
    prior = {
        "kind": "prior_period",
        "input": "measure.jaffle.order_count",
        "offset": {"unit": "year", "value": 1},
    }
    to_date = {"kind": "period_to_date", "input": "measure.jaffle.order_count", "period": "quarter"}
    query = {
        "select": [
            {"expression": prior, "as": "prior_year_orders"},
            {"expression": to_date, "as": "orders_qtd"},
        ],
        "time": MONTHLY,
    }
    rendered = _sql(config, query)
    # The authored default calendar takes no part: the implicit one fills the series.
    assert "implicit_calendar" in rendered and "jaffle_calendar" not in rendered
    assert "LAG(base.m1, 12)" in rendered
    assert "DATE_TRUNC('quarter'" in rendered


@pytest.mark.parametrize(
    ("warehouse", "aggregation", "present", "absent"),
    [
        ("duckdb", {"aggregation": "median"}, "MEDIAN(", None),
        (
            "duckdb",
            {"aggregation": "percentile", "parameters": {"p": 0.95}},
            "QUANTILE_CONT(",
            None,
        ),
        (
            "snowflake",
            {"aggregation": "percentile", "parameters": {"p": 0.95}},
            "PERCENTILE_CONT(0.95) WITHIN GROUP",
            "QUANTILE_CONT(",
        ),
    ],
)
def test_percentile_and_median_render_in_each_dialect(
    config, warehouse, aggregation, present, absent
):
    dialect = replace(config, package=replace(config.package, warehouse=warehouse))
    expression = {"kind": "measure", "measure": "measure.jaffle.item_revenue_usd", **aggregation}
    query = {"select": [{"expression": expression, "as": "item_revenue"}], "time": MONTHLY}
    rendered = compile_query(dialect, Registry(dialect), query)["sql"]
    assert present in rendered
    assert absent is None or absent not in rendered


def test_repeat_customer_orders_join_their_qualified_customers(config):
    query = {
        "select": ["metric.sales.repeat_customer_orders"],
        "group_by": [STORE_NAME],
        "time": MONTHLY,
    }
    rendered = _sql(config, query)
    assert "qualified_customers_by_lifetime_order_count_1" in rendered
    assert "INNER JOIN leaf_1__qualified_customers_by_lifetime_order_count_1" in rendered


def test_contextual_metric_predicates_inherit_time_and_minimal_context_entities(config):
    predicate = {
        "measure": "measure.jaffle.order_count",
        "entity": "entity.jaffle_customer",
        "op": ">",
        "value": 10,
    }
    select = [{"expression": {**SCOPED_COUNT, "predicates": [predicate]}, "as": "orders"}]

    def compiled(grouping: str) -> str:
        query = {"select": select, "group_by": [grouping], "time": MONTHLY}
        return compile_query(config, Registry(config), query)["sql"]

    store_sql = compiled(STORE_NAME)
    assert '"dimension.jaffle_customer_id"' in store_sql
    assert '"dimension.jaffle_store_id"' in store_sql
    assert 'base.t AS "period"' in store_sql
    assert (
        'jaffle_store.store_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_store_id"'
        in store_sql
    )
    source_sql = compiled("dimension.jaffle_customer_type").split(
        "qualified_customers_month_by_order_count_1 AS (", 1
    )[0]
    assert '"dimension.jaffle_customer_id"' in source_sql
    assert "base.t AS t" in source_sql
    assert '"dimension.jaffle_customer_type"' not in source_sql


def test_contextual_metric_predicates_support_coarser_ancestor_grains_and_reject_invalid_mixes(
    runtime,
):
    # A predicate's own grain is the engine's form: a call says only over which rows an
    # entity's aggregate is taken, and refuses the grain. These read the engine's form.
    def engine_validate(grain: str, **parts: object) -> dict:
        call = {"select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
                "time": {"temporal_role": ORDER_TIME, "grain": grain}}  # fmt: skip
        return runtime.validate_internal({**runtime.engine_payload(call), **parts})

    def contextual(time_grain: str) -> list[dict]:
        predicate = {
            "kind": "metric_predicate",
            "entity": "entity.jaffle_customer",
            "scope_mode": "contextual",
            "time_grain": time_grain,
            "input": {"kind": "measure", "measure": "measure.jaffle.order_count"},
            "op": ">",
            "value": 1,
        }
        return [{"expression": predicate, "op": "=", "value": True}]

    coarse = {**SCOPED_COUNT, "predicates": [{"measure": "measure.jaffle.order_count",
              "entity": "entity.jaffle_customer", "op": ">", "value": 10, "time_grain": "month"}]}  # fmt: skip
    assert engine_validate("day", select=[{"expression": coarse, "as": "orders"}])["ok"] is True
    for report in (
        engine_validate("month", metric_filters=contextual("day")),
        engine_validate("week", metric_filters=contextual("month")),
    ):
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "PREDICATE_GRAIN_UNSAFE"


def test_conversion_by_converted_side_product_type_uses_bound_denominator_policy(runtime):
    query = {
        "select": [
            {"expression": "metric.adoption.signup_to_send_conversion_rate_28d", "as": "rate"}
        ],
        "group_by": [PRODUCT_TYPE],
        "time": {"temporal_role": SESSION_TIME, "grain": "month"},
    }
    report = runtime.validate(query)
    assert report["ok"] is True
    rendered = report["explain"]["rendered_sql"]
    assert "conversion_denominator" in rendered
    assert "conversion_numerator" in rendered
    assert "RANK() OVER" in rendered
    assert "INNER JOIN jaffle_item ON jaffle_order.order_id = jaffle_item.order_id" in rendered
    assert "INNER JOIN jaffle_product ON jaffle_item.sku = jaffle_product.sku" in rendered
    order_by = [
        {"field": "period", "direction": "asc"},
        {"field": PRODUCT_TYPE, "direction": "asc"},
    ]
    result = runtime.query({**query, "order_by": order_by})
    assert result["status"] == "ok"
    assert result["row_count"] > 0
    assert all(row[PRODUCT_TYPE] is not None for row in result["rows"])


def test_metric_predicate_subquery_skips_recursive_top_level_compile(config, monkeypatch):
    original_compile_query = compiler_module.compile_query
    compile_calls = 0

    def counted_compile_query(*args, **kwargs):
        nonlocal compile_calls
        compile_calls += 1
        if compile_calls > 1:
            raise AssertionError(
                "metric predicate subqueries should use the AST-only compiler path"
            )
        return original_compile_query(*args, **kwargs)

    monkeypatch.setattr(compiler_module, "compile_query", counted_compile_query)
    compiled = compiler_module.compile_query(
        config,
        Registry(config),
        _engine(config, _entity_filter_query(entity="entity.jaffle_customer", value=1)),
    )
    assert compile_calls == 1
    assert "qualified_customers" in compiled["sql"]


def test_out_of_scope_question_with_enforce_scope_raises_out_of_scope(runtime):
    # Scope enforcement rides beside the call in the engine's payload, never in the call.
    payload = runtime.engine_payload({"select": [REVENUE]})
    report = runtime.validate_internal(
        {**payload, "enforce_scope": True, "question": "Why did revenue decline last quarter?"}
    )
    assert report["ok"] is False
    assert "OUT_OF_SCOPE" in [error["code"] for error in report["errors"]]


def test_supply_cost_by_store_matches_reference_sql(runtime):
    call = {"select": [_measure("item_cost_usd", "supply_cost", "sum")], "group_by": [STORE_NAME]}
    rows = runtime.query(call)["rows"]
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        reference = connection.execute(
            "SELECT s.store_name, SUM(i.item_cost_cents / 100.0) FROM jaffle_item i "
            "JOIN jaffle_order o ON i.order_id = o.order_id "
            "JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY 1"
        ).fetchall()
    assert {row[STORE_NAME]: row["supply_cost"] for row in rows} == pytest.approx(dict(reference))
