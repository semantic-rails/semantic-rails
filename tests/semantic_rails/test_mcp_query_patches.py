"""Query patches returned by the MCP metadata tools are ready-to-send query calls.

discover (with full cards, verbosity="full") and inspect return starter
patches an agent can pass straight to execute, as does build-options (HTTP and
CLI). A patch carries only the call's fields: never the caller's identity,
response options, or the tool's own arguments.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.query_call import FIELDS
from semantic_rails.request_context import RequestContext

QUERY_IR_KEYS = set(FIELDS)
REVENUE = ["measure.jaffle.revenue_usd"]
STORE = "dimension.jaffle_store_name"
ORDER_MONTH = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}
# build-options arguments that reach each of its seven builder steps.
BUILDER_STEPS = {
    "measure": {},
    "aggregation": {"step": "aggregation", "focus_object_id": "measure.jaffle.revenue_usd"},
    "group_by": {"query": {"select": REVENUE}},
    "filter_dimension": {"query": {"select": REVENUE, "group_by": [STORE]}},
    "filter_value": {
        "step": "filter_value",
        "focus_object_id": STORE,
        "query": {"select": REVENUE, "group_by": [STORE]},
    },
    "time": {"step": "time", "query": {"select": REVENUE, "group_by": [STORE]}},
    "review": {"step": "review", "query": {"select": REVENUE, "group_by": [STORE]}},
}
POLICY_CONTEXT = {"environment": "development", "audience": "internal", "roles": ("analyst",)}
CALLS = [
    ("discover", {"terms": "revenue by store", "verbosity": "full"}),
    ("inspect", {"object_id": "measure.jaffle.revenue_usd"}),
]


def _patches(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"starter_query_patch", "query_patch"} and isinstance(value, dict):
                yield value
            yield from _patches(value)
    elif isinstance(node, list):
        for item in node:
            yield from _patches(item)


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


@pytest.mark.parametrize(("tool", "arguments"), CALLS, ids=[name for name, _ in CALLS])
def test_patches_carry_only_query_ir_behind_a_transport_context(
    adapter: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    context = RequestContext(
        request_id="patch-hygiene",
        actor="analyst@example.com",
        tenant="tenant-a",
        roles=("analyst",),
        environment="development",
    )
    response = adapter.call_tool(tool, dict(arguments), request_context=context)
    assert response["ok"], response["errors"]
    for patch in _patches(response):
        assert set(patch) <= QUERY_IR_KEYS, sorted(set(patch) - QUERY_IR_KEYS)


def test_patches_keep_the_callers_partial_query(adapter: SemanticLayerMCPAdapter) -> None:
    partial = {"select": REVENUE}
    response = adapter.call_tool(
        "discover",
        {"terms": "store", "query": partial, "verbosity": "full"},
        request_context=RequestContext(**POLICY_CONTEXT),
    )
    dimension_patches = [row["starter_query_patch"] for row in response["dimensions"]]
    assert dimension_patches
    for patch in dimension_patches:
        assert patch["select"] == partial["select"]
        assert "policy_context" not in patch


@pytest.mark.parametrize(("tool", "arguments"), CALLS, ids=[name for name, _ in CALLS])
def test_every_patch_runs_as_is(
    adapter: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    response = adapter.call_tool(
        tool, {**arguments, "unexpected": 1}, request_context=RequestContext(**POLICY_CONTEXT)
    )
    assert response["ok"], response["errors"]
    for patch in _patches(response):
        assert set(patch) <= QUERY_IR_KEYS, sorted(set(patch) - QUERY_IR_KEYS)
    patches = [patch for patch in _patches(response) if patch.get("select")]
    assert patches
    # Windowed metrics (such as cumulative revenue) carry their default time block.
    for patch in patches:
        validated = adapter.call_tool("execute", {"query": patch, "mode": "validate"})
        assert validated["ok"], (patch, validated["errors"])


@pytest.mark.parametrize("step", list(BUILDER_STEPS))
def test_build_options_patches_are_pure_ir_and_run_at_every_step(
    adapter: SemanticLayerMCPAdapter, step: str
) -> None:
    # MCP has no build-options tool; HTTP shapes the request. Response options
    # and the caller's identity must not leak into the patches.
    payload = {**BUILDER_STEPS[step], "sql_profile": "off"}
    response, status = SemanticHTTPService(adapter.runtime).handle(
        "POST",
        normalize_route("/api/v1/build-options"),
        {**payload, "verbosity": "full"},
        context=RequestContext(**POLICY_CONTEXT),
    )
    assert status == 200, response
    assert response.get("builder_step", step) == step
    patches = list(_patches(response))
    assert patches, f"no patches at step {step}"
    for patch in patches:
        assert set(patch) <= QUERY_IR_KEYS, (step, sorted(set(patch) - QUERY_IR_KEYS))
        if patch.get("select"):
            validated = adapter.call_tool("execute", {"query": patch, "mode": "validate"})
            reasons = [error["details"].get("reason") for error in validated.get("errors", [])]
            if reasons == ["time_valid_hop_without_query_time"]:
                # A grouping read through a time-valid relationship runs once it has a time.
                timed = {**patch, "time": ORDER_MONTH}
                validated = adapter.call_tool("execute", {"query": timed, "mode": "validate"})
            assert validated["ok"], (step, patch, validated["errors"])
