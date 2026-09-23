"""Query patches returned by the MCP metadata tools are pure Query IR.

discover (with full cards, verbosity="compact"), inspect and build-options
return starter patches an agent can pass straight to validate or execute. A patch must carry only Query IR fields: never
the caller's policy context, response options, or the tool's own arguments.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import RequestContext

QUERY_IR_KEYS = {
    "version",
    "select",
    "group_by",
    "where",
    "metric_filters",
    "time",
    "temporal_role_overrides",
    "path_policy",
    "order_by",
    "limit",
    "debug",
    "explain",
    "export",
}
POLICY_CONTEXT = {"environment": "development", "audience": "internal", "roles": ["analyst"]}
CALLS = [
    ("discover", {"terms": "revenue by store", "verbosity": "compact"}),
    ("inspect", {"object_id": "measure.jaffle.revenue_usd"}),
    ("build-options", {"focus_terms": "store"}),
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


def test_patches_run_as_is(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool(
        "discover",
        {
            "terms": "revenue",
            "verbosity": "compact",
            "policy_context": POLICY_CONTEXT,
            "unexpected": 1,
        },
    )
    patch = response["measures"][0]["starter_query_patch"]
    validated = adapter.call_tool("validate", {"query": patch})
    assert validated["ok"], validated["errors"]
