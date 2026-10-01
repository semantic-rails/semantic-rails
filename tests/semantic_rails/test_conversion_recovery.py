"""An agent recovers from a conversion error in one step, and can re-window a conversion metric.

The matching-mode error names the parameter, lists the allowed values with their meaning and
returns the agent's own expression with the parameter set; the conversion metric card shows the
metric's expression, so the same conversion runs over another window at query time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from semantic_rails.expressions import CONVERSION_MATCHING_MODES
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import inspect_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

CONVERSION_METRICS = [
    "metric.adoption.signup_to_send_conversion_rate_28d",
    "metric.sales.session_to_order_conversion_rate_7d",
    "metric.sales.session_to_order_conversion_rate_7d_same_store",
    "metric.sales.adele_then_chai_28d",  # added below: operand filters
]
FILTERED = CONVERSION_METRICS[-1]
PRODUCT = "dimension.jaffle_product_name"
STORE = "dimension.jaffle_store_name"
SESSION_TO_ORDER = {
    "kind": "conversion",
    "base": {"measure": "measure.jaffle.session_starts"},
    "converted": {"measure": "measure.jaffle.order_count"},
    "entity": "entity.jaffle_customer",
    "window": {"unit": "minute", "value": 30},
}


@pytest.fixture(scope="module")
def adapter(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SemanticLayerMCPAdapter]:
    path = copy_package_config(tmp_path_factory.mktemp("conv"), "jaffle_shop", preseed_db=True)
    metrics = path / "metrics" / "extensions" / "advanced_metrics.yml"
    raw = yaml.safe_load(metrics.read_text(encoding="utf-8"))
    raw["metrics"]["sales.adele_then_chai_28d"] = {
        "as": FILTERED,
        "label": "Adele-ade then chai (28d)",
        "kind": "conversion",
        "temporal_role": "temporal_role.jaffle_order_time",
        "expression": {
            "kind": "conversion",
            "entity": "entity.jaffle_customer",
            "window": {"unit": "day", "value": 28},
            "matching_mode": "first_converted_after_base",
            **{  # filter fields written as the dimension's name and label, not its id
                side: {
                    "kind": "aggregate",
                    "measure": "order_count",
                    "filter": {"all": [{"field": field, "op": "=", "value": product}]},
                }
                for side, field, product in (
                    ("base", "jaffle.Product.product_name", "adele-ade"),
                    ("converted", "Product name", "chai and mighty"),
                )
            },
        },
    }
    metrics.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    policies = path / "policies.yml"
    raw = yaml.safe_load(policies.read_text(encoding="utf-8"))
    raw["semantic_policies"] += [
        {"id": "policy.test.hide_product", "kind": "object_visibility", "action": "hidden",
         "object_ids": [PRODUCT], "audiences": ["no_product"]},
        {"id": "policy.test.deny_metric", "kind": "object_access", "action": "deny",
         "object_ids": [FILTERED], "audiences": ["no_metric"]},
    ]  # fmt: skip
    policies.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    runtime = Runtime.from_path(str(path))
    try:
        yield SemanticLayerMCPAdapter(runtime)
    finally:
        runtime.close()


def _run(adapter: SemanticLayerMCPAdapter, expression: dict[str, Any], **extra: Any) -> Any:
    query = {"version": 2, "select": [{"as": "v", "expression": expression}], **extra}
    out = adapter.call_tool("execute", {"mode": "run", "query": query})
    assert out["status"] == "ok", out.get("errors")
    return out["rows"]


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({}, "require 'matching_mode'"),
        ({"matching_mode": "first"}, "unsupported conversion matching_mode 'first'"),
        ({"matching": {"mode": "same_customer"}}, "unsupported conversion matching_mode {"),
    ],
)
def test_matching_mode_error_is_recoverable_in_one_step(adapter, overrides, problem):
    sent = {**SESSION_TO_ORDER, **overrides}
    query = {"version": 2, "select": [{"as": "v", "expression": sent}]}
    error = adapter.call_tool("execute", {"mode": "validate", "query": query})["errors"][0]

    assert error["code"] == "CONVERSION_MATCHING_MODE_REQUIRED"
    assert problem in error["message"]
    assert all(mode in error["message"] for mode in CONVERSION_MATCHING_MODES)
    assert error["details"]["path"] == "expression.matching_mode"
    assert error["details"]["allowed_values"] == CONVERSION_MATCHING_MODES
    (hint,) = error["recovery_hints"]
    assert hint["allowed_values"] == list(CONVERSION_MATCHING_MODES)
    retry = error["details"]["expression"]
    assert retry == {**SESSION_TO_ORDER, "matching_mode": "first_converted_after_base"}
    assert _run(adapter, retry) == [{"v": 0.1}]


@pytest.mark.parametrize("window", [{"minutes": 50}, {"unit": "minute", "value": 0}, 50])
def test_window_error_names_the_units_and_shape(adapter, window):
    sent = {**SESSION_TO_ORDER, "window": window, "matching_mode": "first_converted_after_base"}
    query = {"version": 2, "select": [{"as": "v", "expression": sent}]}
    error = adapter.call_tool("execute", {"mode": "validate", "query": query})["errors"][0]

    assert error["code"] == "CONVERSION_WINDOW_REQUIRED"
    if isinstance(window, dict):
        assert "minute, hour, day, week, month, quarter, year" in error["message"]
        shape = error["details"]["suggested_shape"]
    else:
        (hint,) = error["recovery_hints"]
        shape = hint["suggested_shape"]
    assert shape["unit"].startswith("<minute|hour|day")


@pytest.mark.parametrize("metric_id", CONVERSION_METRICS)
def test_conversion_card_expression_answers_like_the_metric_and_rewindows(adapter, metric_id):
    card = adapter.call_tool("inspect", {"object_id": metric_id, "verbosity": "compact"})["card"]
    expression = card["conversion"]["expression"]
    assert card["conversion"]["matching_modes"] == list(CONVERSION_MATCHING_MODES)

    metric = {"kind": "metric", "metric": metric_id}
    by_store = {"group_by": [STORE], "order_by": [{"field": STORE, "direction": "ASC"}]}
    role = card["default_temporal_role"]
    since_sep_2016 = {
        "time": {
            "temporal_role": role,
            "grain": "month",
            "start": "2016-09-01",
            "end": "2017-07-01",
        },
        "order_by": [{"field": "time", "direction": "ASC"}],
    }
    for extra in ({}, by_store, since_sep_2016):
        rows = _run(adapter, metric, **extra)
        assert rows and _run(adapter, expression, **extra) == rows

    thirty_minutes = {**expression, "window": {"unit": "minute", "value": 30}}
    closest = {**thirty_minutes, "matching_mode": "closest_converted_after_base"}
    assert _run(adapter, thirty_minutes) == _run(adapter, closest) != _run(adapter, metric)


def test_only_conversion_metric_cards_carry_the_conversion_block(adapter):
    card = adapter.call_tool("inspect", {"object_id": "metric.sales.aov_usd"})["card"]
    assert "conversion" not in card


@pytest.mark.parametrize("audience", ["no_product", "no_metric"])
def test_conversion_block_fails_closed_under_a_policy(adapter, audience):
    context = {"policy_context": {"audience": audience}}
    card = inspect_payload(adapter.runtime, object_id=FILTERED, partial_query=context)["card"]
    assert "conversion" not in card
    assert "adele-ade" not in json.dumps(card, default=str)
    assert "conversion" in inspect_payload(adapter.runtime, object_id=FILTERED)["card"]
