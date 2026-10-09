"""Window input filters preserve authorization and independent populations."""

import json
import shutil
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
from semantic_rails.query_call import read_call
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MetricConfig, SemanticPolicyConfig

SHOP = Path("tests/integration/correctness/shop")
REVENUE = "measure.shop.revenue"
CLOCK = "dimension.shop_order_ordered_at"
ROLE = "temporal_role.shop_order_ordered_at"
METRIC = "metric.shop.filtered_history"
CUT = {"field": CLOCK, "op": "gte", "value": "2023-12-01"}
AGGREGATE = {"kind": "aggregate", "measure": REVENUE, "filter": {"all": [CUT]}}
HISTORY = {"kind": "cumulative", "input": REVENUE}


def _metric(expression):
    """A package metric defined by a call's inline expression."""
    engine = read_call({"select": [{"expression": expression, "as": "v"}]})[0]
    parsed = parse_semantic_expression(engine["select"][0]["expression"], context="query")
    return MetricConfig(METRIC, "derived", parsed, temporal_role=ROLE)


@pytest.fixture
def shop_config(tmp_path):
    (tmp_path / "data").mkdir()
    shutil.copyfile(SHOP / "data" / "seed.sql", tmp_path / "data" / "seed.sql")
    return load_package_config(str(SHOP))


def _runtime(config, root):
    return Runtime.from_config(config, source_path=str(root))


def _query(expression=HISTORY):
    return {
        "select": [{"expression": deepcopy(expression), "as": "history"}],
        "time": {"temporal_role": ROLE, "grain": "month"},
    }


@pytest.mark.parametrize("hide_inputs", [False, True], ids=["visible", "hidden-inputs"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("surface", ["validate", "sql", "run"])
@pytest.mark.parametrize("source", ["measure", "policy"])
def test_authorization_precedes_source_refusals(
    shop_config, tmp_path, monkeypatch, verbosity, surface, source, hide_inputs
):
    expression = {**HISTORY, "input": AGGREGATE} if source == "measure" else HISTORY
    metric = _metric(expression)
    policies = [
        SemanticPolicyConfig(
            "policy.shop.deny_history",
            "object_access",
            object_ids=[METRIC],
            audiences=["reader"],
            action="deny",
        ),
    ]
    if hide_inputs:
        policies.append(
            SemanticPolicyConfig(
                "policy.shop.hide_inputs",
                "object_visibility",
                object_ids=[REVENUE, CLOCK],
                audiences=["reader"],
                action="hidden",
            )
        )
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
    runtime = _runtime(config, tmp_path)
    query = _query(METRIC)
    context = RequestContext(audience="reader", attributes={"day": "2023-12-01 02:00:00"})

    def no_output(*args, **kwargs):
        pytest.fail("source refusal reached SQL rendering or warehouse access")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(runtime, "_get_adapter", no_output)
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        arguments = {"query": query, "mode": surface, "verbosity": verbosity}
        actual = adapter.call_tool("execute", arguments, request_context=context)
        with monkeypatch.context() as disabled:
            disabled.setattr(
                compiler, "_validate_restrictive_time_semantics", lambda *args, **kwargs: None
            )
            reference = adapter.call_tool("execute", arguments, request_context=context)
        assert actual["errors"] == reference["errors"]
        # Hidden inputs hide the metric and the clock's time role too: the query names a time
        # role the reader cannot see, refused as the package without them refuses it.
        code = "INVALID_TEMPORAL_ROLE" if hide_inputs else "POLICY_DENIED"
        assert actual["errors"][0]["code"] == code, actual
        for hidden in (REVENUE, CLOCK, "2023-12-01") if hide_inputs else ("2023-12-01",):
            assert hidden not in json.dumps(actual)
        if source == "measure":
            allowed = adapter.call_tool(
                "execute", arguments, request_context=RequestContext(audience="author")
            )
            error = allowed["errors"][0]
            assert error["code"] == "CUMULATIVE_TIME_FILTER_UNSUPPORTED"
            assert error["details"] == {
                "filter_source": "measure",
                "expression": {"kind": "metric", "metric": METRIC},
            }
            assert "2023-12-01" not in json.dumps(allowed)
    finally:
        runtime.close()


@pytest.mark.parametrize("placement", ["select", "result-filter", "arithmetic", "authored"])
@pytest.mark.parametrize("scope", ["full-history", "query-period"])
def test_sibling_filter_keeps_independent_window_answers(shop_config, tmp_path, placement, scope):
    history = deepcopy(HISTORY)
    if scope == "query-period":
        history["window_scope"] = "query_period"
    query = _query(history)
    if placement == "select":
        query["select"].append({"expression": AGGREGATE, "as": "recent"})
    elif placement == "result-filter":
        query["select"].append({"expression": AGGREGATE, "as": "recent"})
        query["having"] = [{"field": "recent", "op": "gt", "value": 0}]
    else:
        expression = {"kind": "arithmetic", "op": "+", "left": history, "right": AGGREGATE}
        if placement == "authored":
            metric = _metric(expression)
            shop_config = replace(shop_config, metric_recipes=[*shop_config.metric_recipes, metric])
            expression = METRIC
        query = _query(expression)
    runtime = _runtime(shop_config, tmp_path)
    try:
        rows = runtime.query(query)["rows"]
        december = next(row for row in rows if str(row["period"]).startswith("2023-12-01"))
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


@pytest.mark.parametrize("unused_attribute", ["day", "store"])
def test_unused_clock_policy_keeps_applied_store_population(
    shop_config, tmp_path, unused_attribute
):
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
                    "attribute": unused_attribute,
                },
            ),
        ],
    )
    runtime = _runtime(config, tmp_path)
    query = _query()
    reader = RequestContext(audience="reader", attributes={"store": "a", "day": "2024-01-01"})
    try:
        compiled = runtime.compile(query, context=reader)
        assert compiled["status"] == "ok"
        assert "account_days" not in compiled["rendered_sql"]
        rows = runtime.query(query, context=reader)["rows"]
        december = next(row for row in rows if str(row["period"]).startswith("2023-12-01"))
        with runtime._get_adapter()._db.conn.cursor() as connection:
            reference = connection.execute(
                "select sum(amount) from orders where store_id = 'a' and ordered_at < '2024-01-01'"
            ).fetchone()[0]
        assert Decimal(december["history"]) == reference == 17
    finally:
        runtime.close()


def test_source_refusal_message_has_no_space_before_period(shop_config, tmp_path):
    expression = {"kind": "rolling", "input": AGGREGATE, "window": {"unit": "month", "value": 2}}
    runtime = _runtime(shop_config, tmp_path)
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
