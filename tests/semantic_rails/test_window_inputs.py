"""Summing windows preserve additive parts and refuse statistics before execution."""

from dataclasses import replace

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.bind import lift_conditional_aggregates
from semantic_rails.compiler_parts.post_aggregation import (
    _as_offset_window_expr,
    _compile_offset_window_expr,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import OffsetWindowExpr, parse_semantic_expression
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.registry import Registry
from semantic_rails.schema import MetricConfig
from semantic_rails.sql_ast import SqlCall, SqlWindow

ROLE = "temporal_role.jaffle_order_time"
REVENUE = {"measure": "measure.jaffle.revenue_usd"}
ORDERS = {"measure": "measure.jaffle.order_count"}
RATIO = {"kind": "ratio", "numerator": REVENUE, "denominator": ORDERS}
CONDITIONAL_COUNT = {
    "kind": "aggregate_if",
    "aggregation": "count",
    "value": {"kind": "column", "column": "customer_id", "entity": "entity.jaffle_order"},
    "condition": {
        "kind": "comparison",
        "op": "=",
        "left": {"kind": "column", "column": "status", "entity": "entity.jaffle_order"},
        "right": {"kind": "literal", "value": "completed"},
    },
}


def window(input_expr, kind="rolling"):
    options = {"window": {"unit": "month", "value": 3}} if kind == "rolling" else {}
    if kind == "period_to_date":
        options = {"period": "year"}
    return {"kind": kind, "input": input_expr, **options}


def query(expression, **extra):
    return {
        "select": [{"as": "value", "expression": expression}],
        "time": {"temporal_role": ROLE, "grain": "month"},
        **extra,
    }


@pytest.fixture
def config(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    return config


def lower_conditional_window(config, expression):
    # Synthetic measures have no authored clock. Exercise the shared lowering guard
    # directly so the independent temporal-binding refusal cannot hide this input rule.
    rewritten, synthetics = lift_conditional_aggregates(normalize_query(query(expression)), config)
    config = replace(config, measures=[*config.measures, *synthetics.values()])
    expr = _as_offset_window_expr(rewritten.select[0].expression)
    assert expr is not None
    return _compile_offset_window_expr(
        expr, config, time_alias="t", group_aliases=[], query_grain="month", table_alias="base"
    )


@pytest.mark.parametrize("kind", ["rolling", "cumulative", "period_to_date"])
def test_conditional_count_still_feeds_a_summing_window(config, kind):
    result = lower_conditional_window(config, window(CONDITIONAL_COUNT, kind))
    assert isinstance(result, SqlWindow)
    assert isinstance(result.function, SqlCall) and result.function.name == "SUM"


@pytest.mark.parametrize("kind", ["rolling", "cumulative", "period_to_date"])
@pytest.mark.parametrize(
    "input_expr",
    [
        RATIO,
        {"kind": "arithmetic", "op": "divide", "left": REVENUE, "right": ORDERS},
        {"metric": "metric.sales.aov_usd"},
    ],
    ids=["ratio", "divide", "recipe"],
)
def test_ratio_windows_sum_each_part(config, kind, input_expr):
    sql = compile_query(config, Registry(config), query(window(input_expr, kind)))["sql"]
    assert "SUM(base.m1) OVER" in sql
    assert "NULLIF(SUM(base.m2) OVER" in sql
    assert "SUM(base.m1 /" not in sql


UNSAFE = [
    *[
        ({**REVENUE, "aggregation": agg}, agg)
        for agg in ["avg", "min", "max", "median", "percentile"]
    ],
    ({"measure": "measure.jaffle.inventory_on_hand_eop"}, "inventory_on_hand_eop"),
    ({"measure": "measure.jaffle.customer_count"}, "customer_count"),
    ({**CONDITIONAL_COUNT, "aggregation": "count_distinct"}, "count_distinct"),
    (
        {
            "kind": "distribution",
            "function": "median",
            "over": {"kind": "entity_value", "entity": "entity.jaffle_order", "input": REVENUE},
        },
        "distribution",
    ),
    ({"kind": "arithmetic", "op": "multiply", "left": REVENUE, "right": ORDERS}, "multiply"),
    ({"kind": "arithmetic", "op": "add", "left": RATIO, "right": REVENUE}, "ratio"),
    ({"kind": "ratio", "numerator": RATIO, "denominator": ORDERS}, "ratio"),
    (window(REVENUE), "rolling"),
]


@pytest.mark.parametrize(("input_expr", "name"), UNSAFE)
def test_non_additive_inputs_refuse_centrally(config, input_expr, name):
    if input_expr.get("aggregation") == "percentile":
        input_expr = {**input_expr, "parameters": {"p": 0.5}}
    # The outer aggregation is valid in isolation: it is the summing window that refuses.
    if "aggregation" in input_expr:
        config = replace(
            config,
            measures=[
                replace(
                    m, allowed_aggregations=[*m.allowed_aggregations, input_expr["aggregation"]]
                )
                if m.id == REVENUE["measure"]
                else m
                for m in config.measures
            ],
        )
    payload = query(window(input_expr))
    measure = next((m for m in config.measures if m.id == input_expr.get("measure")), None)
    if measure and measure.default_temporal_role:
        payload["time"]["temporal_role"] = measure.default_temporal_role
    with pytest.raises(SemanticLayerError) as raised:
        if input_expr.get("kind") == "aggregate_if":
            lower_conditional_window(config, payload["select"][0]["expression"])
        else:
            compile_query(config, Registry(config), payload)
    assert raised.value.code == "ROLLUP_UNSAFE"
    assert name in str(raised.value)
    assert raised.value.details["recovery_hints"]


@pytest.mark.parametrize("route", ["select", "recipe", "derived", "metric_filter"])
def test_recipes_and_filters_cannot_bypass_the_guard(config, route):
    invalid = window({**REVENUE, "aggregation": "max"})
    config = replace(
        config,
        measures=[
            replace(m, allowed_aggregations=[*m.allowed_aggregations, "max"])
            if m.id == REVENUE["measure"]
            else m
            for m in config.measures
        ],
    )
    recipe = MetricConfig(
        id="metric.window_statistic",
        kind="derived",
        expression=parse_semantic_expression(invalid, context="query"),
    )
    derived = MetricConfig(
        id="metric.derived_statistic",
        kind="derived",
        expression=parse_semantic_expression({"metric": recipe.id}, context="query"),
    )
    config = replace(config, metric_recipes=[*config.metric_recipes, recipe, derived])
    expression = (
        invalid
        if route in {"select", "metric_filter"}
        else {"metric": derived.id if route == "derived" else recipe.id}
    )
    payload = query(expression)
    if route == "metric_filter":
        payload = query(REVENUE, metric_filters=[{"expression": expression, "op": ">", "value": 0}])
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), payload)
    assert raised.value.code == "ROLLUP_UNSAFE"


def test_direct_window_lowering_cannot_bypass_the_guard(config):
    expr = OffsetWindowExpr(
        input=parse_semantic_expression({**REVENUE, "aggregation": "max"}, context="query"),
        kind="rolling",
        aggregate="sum",
        unit="month",
        value=3,
    )
    with pytest.raises(SemanticLayerError) as raised:
        _compile_offset_window_expr(
            expr, config, time_alias="t", group_aliases=[], query_grain="month", table_alias="base"
        )
    assert raised.value.code == "ROLLUP_UNSAFE"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("input_expr", [RATIO, {"measure": "measure.jaffle.inventory_on_hand_eop"}])
def test_execute_uses_the_same_window_rule(runtime_factory, transport, input_expr, monkeypatch):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = query(window(input_expr))
        stock = input_expr != RATIO
        if stock:
            payload["time"]["temporal_role"] = "temporal_role.jaffle_inventory_day"
            monkeypatch.setattr(runtime, "_get_adapter", lambda: pytest.fail("refused before SQL"))
        if transport == "rest":
            service = SemanticHTTPService(runtime)
            try:
                response, status = service.handle("POST", "/query", payload)
            except SemanticLayerError as exc:
                response, status = service.exception_payload(exc, stage="query")
            assert status == (400 if stock else 200)
        else:
            response = SemanticLayerMCPAdapter(runtime).call_tool(
                "execute", {"query": payload, "verbosity": "compact"}
            )
        if stock:
            assert response["errors"][0]["code"] == "ROLLUP_UNSAFE"
        else:
            assert response["ok"]
            assert "NULLIF(SUM(base.m2) OVER" in response["rendered_sql"]
    finally:
        runtime.close()


def test_prior_period_still_reads_a_statistic(config):
    payload = query(
        {
            "kind": "prior_period",
            "input": {**REVENUE, "aggregation": "max"},
            "offset": {"unit": "month", "value": 1},
        }
    )
    config = replace(
        config,
        measures=[
            replace(m, allowed_aggregations=[*m.allowed_aggregations, "max"])
            if m.id == REVENUE["measure"]
            else m
            for m in config.measures
        ],
    )
    assert "LAG(base.m1, 1) OVER" in compile_query(config, Registry(config), payload)["sql"]


@pytest.mark.parametrize("route", ["recipe", "derived", "metric_filter"])
def test_ratio_rewrite_survives_recipe_and_filter_indirection(config, route):
    recipe = MetricConfig(
        id="metric.running_ratio",
        kind="derived",
        expression=parse_semantic_expression(window(RATIO), context="query"),
    )
    derived = MetricConfig(
        id="metric.derived_ratio",
        kind="derived",
        expression=parse_semantic_expression({"metric": recipe.id}, context="query"),
    )
    config = replace(config, metric_recipes=[*config.metric_recipes, recipe, derived])
    expression = {"metric": derived.id if route == "derived" else recipe.id}
    payload = query(expression)
    if route == "metric_filter":
        payload = query(
            REVENUE, metric_filters=[{"expression": expression, "op": ">", "value": 10}]
        )
    sql = compile_query(config, Registry(config), payload)["sql"]
    assert "SUM(base.m1 /" not in sql
    assert "NULLIF(SUM(base.m2) OVER" in sql


@pytest.mark.parametrize("route", ["recipe", "derived", "metric_filter", "role_calendar"])
def test_fiscal_period_to_date_cannot_hide_behind_another_expression(config, route):
    recipe = MetricConfig(
        id="metric.year_to_date",
        kind="derived",
        expression=parse_semantic_expression(window(REVENUE, "period_to_date"), context="query"),
    )
    derived = MetricConfig(
        id="metric.derived_year_to_date",
        kind="derived",
        expression=parse_semantic_expression({"metric": recipe.id}, context="query"),
    )
    config = replace(config, metric_recipes=[*config.metric_recipes, recipe, derived])
    expression = {"metric": derived.id if route == "derived" else recipe.id}
    payload = query(expression)
    if route == "metric_filter":
        payload = query(
            REVENUE, metric_filters=[{"expression": expression, "op": ">", "value": 10}]
        )
    payload["time"].update(calendar_id="fiscal", fill=True)
    if route == "role_calendar":
        config = replace(
            config,
            entities=[
                replace(e, calendar_id="fiscal") if e.id == "entity.jaffle_order" else e
                for e in config.entities
            ],
        )
        payload["time"].update(calendar_id="default", fill=False)
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), payload)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details["calendar_id"] == "fiscal"


def test_zero_windowed_denominator_stays_null(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        input_expr = {
            "kind": "ratio",
            "numerator": REVENUE,
            "denominator": {
                "kind": "arithmetic",
                "op": "subtract",
                "left": ORDERS,
                "right": ORDERS,
            },
        }
        result = runtime.query(query(window(input_expr)))
        assert result["row_count"] > 0
        assert all(row["value"] is None for row in result["rows"])
    finally:
        runtime.close()
