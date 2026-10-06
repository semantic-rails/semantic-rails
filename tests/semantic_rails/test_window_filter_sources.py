"""Window input filters preserve authorization and independent populations."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from semantic_rails import compiler
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MetricConfig, SemanticPolicyConfig

SHOP = Path("tests/integration/correctness/shop")
REVENUE = "measure.shop.revenue"
CLOCK = "dimension.shop_order_ordered_at"
ROLE = "temporal_role.shop_order_ordered_at"
METRIC = "metric.shop.filtered_history"
CUT = {"field": CLOCK, "op": ">=", "value": "2023-12-01"}
AGGREGATE = {"kind": "aggregate", "measure": REVENUE, "filter": {"all": [CUT]}}
HISTORY = {"kind": "cumulative", "input": {"measure": REVENUE}}


@pytest.fixture
def shop_config():
    config = load_package_config(str(SHOP))
    return replace(config, package=replace(config.package, default_db=":memory:"))


def _runtime(config):
    return Runtime.from_config(config, source_path=str(SHOP))


def _query(expression=HISTORY):
    return {
        "version": 1,
        "select": [{"expression": deepcopy(expression), "as": "history"}],
        "time": {"temporal_role": ROLE, "grain": "month"},
    }


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("surface", ["validate", "sql", "run"])
@pytest.mark.parametrize("source", ["measure", "policy"])
def test_authorization_precedes_source_refusals(
    shop_config, monkeypatch, verbosity, surface, source
):
    expression = {**HISTORY, "input": AGGREGATE} if source == "measure" else HISTORY
    metric = MetricConfig(
        METRIC,
        "derived",
        parse_semantic_expression(expression, context="query"),
        temporal_role=ROLE,
    )
    policies = [
        SemanticPolicyConfig(
            "policy.shop.deny_history",
            "object_access",
            object_ids=[METRIC],
            audiences=["reader"],
            action="deny",
        ),
        SemanticPolicyConfig(
            "policy.shop.hide_inputs",
            "object_visibility",
            object_ids=[REVENUE, CLOCK],
            audiences=["reader"],
            action="hidden",
        ),
    ]
    dimensions = shop_config.dimensions
    if source == "policy":
        clock = next(row for row in dimensions if row.id == CLOCK)
        dimensions = [
            *dimensions,
            replace(clock, id="dimension.shop_order_clock_label", data_type="string"),
        ]
        policies.append(
            SemanticPolicyConfig(
                "policy.shop.visible_day",
                "row_filter",
                audiences=["reader"],
                config={"dimension": "dimension.shop_order_clock_label", "attribute": "day"},
            )
        )
    config = replace(
        shop_config,
        dimensions=dimensions,
        metric_recipes=[*shop_config.metric_recipes, metric],
        semantic_policies=policies,
    )
    runtime = _runtime(config)
    query = {**_query({"metric": METRIC}), "verbosity": verbosity}
    context = RequestContext(audience="reader", attributes={"day": "2023-12-01 02:00:00"})

    def no_output(*args, **kwargs):
        pytest.fail("source refusal reached SQL rendering or warehouse access")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(runtime, "_get_adapter", no_output)
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        arguments = {"query": query, "mode": surface}
        actual = adapter.call_tool("execute", arguments, request_context=context)
        with monkeypatch.context() as disabled:
            disabled.setattr(
                compiler, "_validate_restrictive_time_semantics", lambda *args, **kwargs: None
            )
            reference = adapter.call_tool("execute", arguments, request_context=context)
        assert actual["errors"] == reference["errors"]
        assert actual["errors"][0]["code"] == "POLICY_DENIED", actual
        for hidden in (REVENUE, CLOCK, "2023-12-01"):
            assert hidden not in json.dumps(actual)
        if source == "measure":
            allowed = adapter.call_tool(
                "execute", arguments, request_context=RequestContext(audience="author")
            )
            error = allowed["errors"][0]
            assert error["code"] == "CUMULATIVE_TIME_FILTER_UNSUPPORTED"
            assert error["details"] == {
                "filter_source": "measure",
                "expression": {"metric": METRIC},
            }
            assert "2023-12-01" not in json.dumps(allowed)
    finally:
        runtime.close()


@pytest.mark.parametrize("placement", ["select", "metric-filter", "arithmetic", "authored"])
@pytest.mark.parametrize("scope", ["full-history", "query-period"])
def test_sibling_filter_keeps_independent_window_answers(shop_config, placement, scope):
    history = deepcopy(HISTORY)
    if scope == "query-period":
        history["window_scope"] = "query_period"
    query = _query(history)
    if placement == "select":
        query["select"].append({"expression": AGGREGATE, "as": "recent"})
    elif placement == "metric-filter":
        query["metric_filters"] = [{"expression": AGGREGATE, "op": ">", "value": 0}]
    else:
        expression = {"kind": "arithmetic", "op": "+", "left": history, "right": AGGREGATE}
        if placement == "authored":
            metric = MetricConfig(
                METRIC,
                "derived",
                parse_semantic_expression(expression, context="query"),
                temporal_role=ROLE,
            )
            shop_config = replace(shop_config, metric_recipes=[*shop_config.metric_recipes, metric])
            expression = {"metric": METRIC}
        query = _query(expression)
    runtime = _runtime(shop_config)
    try:
        rows = runtime.query(query)["rows"]
        december = next(row for row in rows if str(row[f"{ROLE}__month"]).startswith("2023-12-01"))
        with runtime._get_adapter()._db.conn.cursor() as connection:
            reference = connection.execute(
                "select sum(amount), sum(case when ordered_at >= '2023-12-01' then amount end) "
                "from orders where ordered_at < '2024-01-01'"
            ).fetchone()
        assert reference == (22, 7)
        assert Decimal(december["history"]) == (
            sum(reference) if placement in {"arithmetic", "authored"} else reference[0]
        )
        if placement == "select":
            assert Decimal(december["recent"]) == reference[1]
    finally:
        runtime.close()


def test_unused_clock_policy_keeps_applied_store_population(shop_config):
    snapshot = next(
        row for row in shop_config.dimensions if row.id == "dimension.shop_account_day_snapshot_day"
    )
    config = replace(
        shop_config,
        dimensions=[
            *shop_config.dimensions,
            replace(snapshot, id="dimension.shop_account_day_snapshot_label", data_type="string"),
        ],
        semantic_policies=[
            SemanticPolicyConfig(
                "policy.shop.store",
                "row_filter",
                audiences=["reader"],
                config={"dimension": "dimension.shop_order_store_id", "attribute": "store"},
            ),
            SemanticPolicyConfig(
                "policy.shop.snapshot",
                "row_filter",
                audiences=["reader"],
                config={
                    "dimension": "dimension.shop_account_day_snapshot_label",
                    "attribute": "day",
                },
            ),
        ],
    )
    runtime = _runtime(config)
    query = _query()
    query["policy_context"] = RequestContext(
        audience="reader", attributes={"store": "a", "day": "2024-01-01"}
    ).to_policy_context()
    try:
        compiled = runtime.compile(query)
        assert compiled["status"] == "ok"
        assert "account_days" not in compiled["sql"]
        rows = runtime.query(query)["rows"]
        december = next(row for row in rows if str(row[f"{ROLE}__month"]).startswith("2023-12-01"))
        with runtime._get_adapter()._db.conn.cursor() as connection:
            reference = connection.execute(
                "select sum(amount) from orders where store_id = 'a' and ordered_at < '2024-01-01'"
            ).fetchone()[0]
        assert Decimal(december["history"]) == reference == 17
    finally:
        runtime.close()


def test_source_refusal_message_has_no_space_before_period(shop_config):
    expression = {"kind": "rolling", "input": AGGREGATE, "window": {"unit": "month", "value": 2}}
    runtime = _runtime(shop_config)
    try:
        with pytest.raises(SemanticLayerError) as caught:
            runtime.compile(_query(expression))
        assert str(caught.value) == (
            "Rolling, prior_period, and period_to_date expressions do not support a measure filter "
            "on a temporal column (measure_filters[0]): the leaf-level WHERE truncates the lookback "
            "rows the window function depends on, producing silently wrong values. "
            "Query an unwindowed measure or ask the package author for a supported metric."
        )
    finally:
        runtime.close()
