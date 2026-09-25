"""Query patches returned by the MCP metadata tools are pure Query IR.

discover (with full cards, verbosity="compact") and inspect return starter
patches an agent can pass straight to execute, as does build-options (HTTP and
CLI). A patch must carry only Query IR fields: never the caller's policy
context, response options, or the tool's own arguments.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import _QUERY_IR_KEYS, build_options_payload
from semantic_rails.request_context import RequestContext

QUERY_IR_KEYS = set(_QUERY_IR_KEYS)
REVENUE = [{"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}]
STORE = "dimension.jaffle_store_name"
# build-options arguments that reach each of its seven builder steps.
BUILDER_STEPS = {
    "measure": {},
    "aggregation": {"step": "aggregation", "focus_object_id": "measure.jaffle.revenue_usd"},
    "group_by": {"query": {"version": 2, "select": REVENUE}},
    "filter_dimension": {"query": {"version": 2, "select": REVENUE, "group_by": [STORE]}},
    "filter_value": {
        "step": "filter_value",
        "focus_object_id": STORE,
        "query": {"version": 2, "select": REVENUE, "group_by": [STORE]},
    },
    "time": {"step": "time", "query": {"version": 2, "select": REVENUE, "group_by": [STORE]}},
    "review": {"step": "review", "query": {"version": 2, "select": REVENUE, "group_by": [STORE]}},
}
POLICY_CONTEXT = {"environment": "development", "audience": "internal", "roles": ["analyst"]}
CALLS = [
    ("discover", {"terms": "revenue by store", "verbosity": "compact"}),
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
def test_patches_carry_only_query_ir_for_local_callers(
    adapter: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    response = adapter.call_tool(
        tool, {**arguments, "policy_context": POLICY_CONTEXT, "unexpected": 1}
    )
    assert response["ok"], response["errors"]
    patches = list(_patches(response))
    assert patches, f"{tool} returned no query patches"
    for patch in patches:
        assert set(patch) <= QUERY_IR_KEYS, sorted(set(patch) - QUERY_IR_KEYS)


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
    partial = {
        "version": 2,
        "select": [{"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
        "policy_context": POLICY_CONTEXT,
    }
    response = adapter.call_tool(
        "discover", {"terms": "store", "query": partial, "verbosity": "compact"}
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
        tool, {**arguments, "policy_context": POLICY_CONTEXT, "unexpected": 1}
    )
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
    # Response options and a policy context must not leak into the patches.
    arguments = dict(BUILDER_STEPS[step])
    query = {
        **arguments.pop("query", {}),
        "policy_context": POLICY_CONTEXT,
        "verbosity": "full",
        "sql_profile": "off",
    }
    response = build_options_payload(
        adapter.runtime, partial_query=query, verbosity="full", **arguments
    )
    assert response.get("builder_step", step) == step
    patches = list(_patches(response))
    assert patches, f"no patches at step {step}"
    for patch in patches:
        assert set(patch) <= QUERY_IR_KEYS, (step, sorted(set(patch) - QUERY_IR_KEYS))
        if patch.get("select"):
            validated = adapter.call_tool("execute", {"query": patch, "mode": "validate"})
            assert validated["ok"], (step, patch, validated["errors"])
