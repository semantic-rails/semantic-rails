"""Authenticated HTTP plans return portable Query IR without losing policy checks."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.mcp_server import MCP_PROTOCOL_VERSION
from semantic_rails.request_context import (
    RequestContext,
    extract_bearer_or_api_key,
    get_policy_context_resolver,
    set_policy_context_resolver,
)
from semantic_rails.schema import SemanticPolicyConfig


@pytest.fixture
def governed_app(package_config_factory, monkeypatch):
    class IdentityResolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            # Authenticated credentials select the identity; caller policy
            # claims and semantic headers cannot change it.
            key = extract_bearer_or_api_key(headers)
            return RequestContext(
                request_id=request_id,
                actor="planner-user",
                tenant="planner-tenant",
                roles=("analyst",),
                environment="production" if key == "test-plan-prod" else "development",
            )

    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-plan-dev,test-plan-prod")
    previous_resolver = get_policy_context_resolver()
    set_policy_context_resolver(IdentityResolver())
    _, package_path = package_config_factory("jaffle_shop")
    app = SemanticLayerASGIApp(path=str(package_path), max_workers=1)
    app.runtime.config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.test.production_revenue_hold",
            kind="object_access",
            object_ids=["measure.jaffle.revenue_usd"],
            roles=["analyst"],
            environments=["production"],
            action="deny",
        )
    )
    try:
        yield app
    finally:
        asyncio.run(app.aclose())
        set_policy_context_resolver(previous_resolver)


def _call(app, transport, operation, arguments, *, key="test-plan-dev"):
    async def request():
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "X-Semantic-Environment": "spoofed-environment",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = arguments
        path = f"/api/v1/{operation}"
        if transport == "mcp":
            path = "/mcp"
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": operation, "arguments": arguments},
            }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(path, json=body, headers=headers)
        payload = response.json()
        if transport == "mcp" and response.status_code == 200:
            payload = payload["result"]["structuredContent"]
        return response.status_code, payload

    return asyncio.run(request())


def _query_irs(payload):
    """Include every returned draft and the advertised validation input."""
    rows = [payload["best"], *payload.get("alternatives", []), *payload.get("blocked", [])]
    queries = [row["query_ir"] for row in rows if row and "query_ir" in row]
    if "validate" in payload.get("next", {}):
        queries.append(payload["next"]["validate"]["query"])
    return queries


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("detail", ["query", "best", "full"])
@pytest.mark.parametrize("partial", [False, True])
def test_authenticated_plan_queries_round_trip_to_compile(governed_app, transport, detail, partial):
    arguments = {"intent": "top stores by revenue", "detail": detail, "limit": 3}
    if partial:
        arguments["query"] = {"version": 1, "limit": 17}

    status, plan = _call(governed_app, transport, "plan", arguments)
    assert status == 200
    assert plan["ok"] is True
    assert plan["status"] == "ok", plan.get("why")
    assert plan["best"]["validation_ok"] is True
    assert plan["request_context"]["tenant"] == "planner-tenant"
    queries = _query_irs(plan)
    assert queries
    for query in queries:
        assert (
            not {"policy_context", "request_context", "request_id", "intent", "detail"}
            & query.keys()
        )
    query = plan["best"]["query_ir"]
    if partial:
        assert query["limit"] == 17  # The planner's alternative cap is not a query limit.

    # Preserve both supported compile input shapes, including flat Query IR.
    for body in (query, {"query": query}):
        status, compiled = _call(governed_app, transport, "compile", body)
        assert status == 200
        assert compiled["ok"] is True, compiled
        assert compiled["rendered_sql"]
        assert compiled["request_context"]["environment"] == "development"

    # A portable plan is not authority to bypass a later caller's policy.
    status, denied = _call(
        governed_app, transport, "compile", {"query": query}, key="test-plan-prod"
    )
    assert status == (400 if transport == "rest" else 200)
    assert denied["ok"] is False
    assert denied["error"]["code"] == "POLICY_DENIED"
    assert not denied.get("rendered_sql")


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("partial", [False, True])
def test_plan_validates_with_trusted_policy_despite_caller_spoofs(governed_app, transport, partial):
    spoof = {"actor": "spoofed-user", "roles": ["admin"], "environment": "development"}
    arguments = {
        "intent": "top stores by revenue",
        "detail": "full",
        "limit": 3,
        "policy_context": spoof,
    }
    if partial:
        arguments["query"] = {
            "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}],
            "policy_context": spoof,
        }
    status, plan = _call(governed_app, transport, "plan", arguments, key="test-plan-prod")
    assert status == 200
    assert plan["status"] == "low_confidence"
    assert plan["best"]["validation_ok"] is False
    assert not plan["next"].get("ready_for")
    assert plan["request_context"]["environment"] == "production"
    assert plan["request_context"]["roles"] == ["analyst"]
    assert "spoofed-user" not in json.dumps(plan)
    for query in _query_irs(plan):
        assert "policy_context" not in query

    # A legal fallback for a different revenue measure can make the planner
    # explain semantic drift first. Follow its public recovery path and prove
    # the selected draft failed policy, not query structure or caller claims.
    query = plan["best"]["query_ir"]
    status, denied = _call(
        governed_app, transport, "validate", {"query": query}, key="test-plan-prod"
    )
    assert status == 200
    assert denied["ok"] is False
    assert any(error["code"] == "POLICY_DENIED" for error in denied["errors"])
    status, allowed = _call(governed_app, transport, "validate", {"query": query})
    assert status == 200
    assert allowed["ok"] is True


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_plan_requires_configured_authentication(governed_app, transport):
    status, _ = _call(governed_app, transport, "plan", {"intent": "revenue"}, key="")
    assert status == 401
