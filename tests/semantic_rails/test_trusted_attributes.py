"""Host-supplied trusted attributes: typed, immutable, carried inside, never public."""

from __future__ import annotations

import asyncio
import copy
import json
import pickle

import httpx
import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.embedding import RequestContext, TrustedAttributes
from semantic_rails.mcp_server import MCP_PROTOCOL_VERSION
from semantic_rails.request_context import (
    context_from_headers,
    context_from_policy_context,
    extract_bearer_or_api_key,
    get_audit_sink,
    get_policy_context_resolver,
    request_context_payload,
    set_audit_sink,
    set_policy_context_resolver,
)
from tests.semantic_rails.conftest import copy_package_config

CANARY = "canary-7731-a1"
SPOOF = {"customer_id": "spoofed-customer"}
HOST_ATTRIBUTES = TrustedAttributes({"customer_id": CANARY, "regions": ["eu", "us"]})


class _Text(str):
    pass


def test_host_attributes_are_typed_immutable_and_opaque():
    attributes = TrustedAttributes(
        {"customer_id": CANARY, "regions": ["eu", "us"], "tier": 3, "internal": False}
    )
    assert attributes.names == ("customer_id", "internal", "regions", "tier")
    assert attributes.get("regions") == ("eu", "us")
    assert attributes.get("tier") == 3 and attributes.get("internal") is False
    assert attributes.get("missing") is None and "missing" not in attributes
    with pytest.raises(TypeError):
        attributes._values["customer_id"] = "other"  # type: ignore[index]
    with pytest.raises(AttributeError):
        attributes._values = {}  # type: ignore[misc]
    # Not a mapping or iterable: generic serializers can't expand it into values.
    for expose in (dict, list, vars, json.dumps):
        with pytest.raises(TypeError):
            expose(attributes)
    assert CANARY not in repr(attributes)
    assert copy.deepcopy(attributes) == attributes == pickle.loads(pickle.dumps(attributes))
    assert TrustedAttributes({"tier": 1}) != TrustedAttributes({"tier": True})
    host_mapping = RequestContext(attributes={"customer_id": CANARY}).attributes
    assert host_mapping == TrustedAttributes({"customer_id": CANARY})


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({"customer_id": 1.5}, TypeError),
        ({"customer_id": None}, TypeError),
        ({"customer_id": ""}, TypeError),
        ({"customer_id": CANARY.encode()}, TypeError),
        ({"customer_id": {"id": CANARY}}, TypeError),
        ({"customer_id": {CANARY}}, TypeError),
        ({"customer_id": _Text(CANARY)}, TypeError),
        ({"customer_id": []}, TypeError),
        ({"customer_id": [CANARY, 7]}, TypeError),
        ({"customer_id": [[CANARY]]}, TypeError),
        ({"customer_id": [CANARY, ""]}, TypeError),
        ({"Customer-ID": CANARY}, ValueError),
        ({7: CANARY}, ValueError),
        ([("customer_id", CANARY)], TypeError),
    ],
)
def test_unknown_or_wrong_types_fail_without_echoing_values(values, error):
    with pytest.raises(error) as raised:
        RequestContext(attributes=values)
    assert CANARY not in str(raised.value)


def test_contexts_without_attributes_are_unchanged():
    context = RequestContext(request_id="r1", actor="a", roles=("analyst",), environment="prod")
    assert context.to_policy_context() == {
        "actor": "a",
        "roles": ["analyst"],
        "environment": "prod",
    }
    assert context.to_public_dict() == {"request_id": "r1", **context.to_policy_context()}
    assert context_from_policy_context(context.to_policy_context(), request_id="r1") == context


def test_attributes_survive_internal_round_trips_and_stay_private():
    context = RequestContext(request_id="r1", actor="a", attributes=HOST_ATTRIBUTES)
    carried = context.to_policy_context()
    for _ in range(3):
        carried = context_from_policy_context(carried).to_policy_context()
    assert carried["attributes"] is HOST_ATTRIBUTES
    assert context_from_policy_context(carried, request_id="r1") == context
    assert context.to_public_dict() == {"request_id": "r1", "actor": "a"}
    assert request_context_payload(carried) == {"actor": "a"}
    assert CANARY not in repr(context) + json.dumps(carried, default=str)


@pytest.mark.parametrize("value", [SPOOF, repr(HOST_ATTRIBUTES), [["customer_id", "x"]]])
def test_caller_values_cannot_create_attributes(value):
    body = {"policy_context": {"actor": "local", "attributes": value}}
    headers = {"X-Semantic-Attributes": json.dumps(SPOOF), "X-Attributes": "customer_id=x"}
    assert not context_from_policy_context(body["policy_context"]).attributes
    resolved = context_from_headers(headers, payload=body)
    assert resolved.actor == "local"
    assert not resolved.attributes


@pytest.fixture
def host_app(tmp_path, monkeypatch):
    class HostResolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            assert extract_bearer_or_api_key(headers) == "test-attributes-key"
            return RequestContext(
                request_id=request_id,
                actor="end-user",
                tenant="tenant-a",
                roles=("analyst",),
                attributes=HOST_ATTRIBUTES,
            )

    class ListSink:
        def emit(self, payload):
            events.append(payload)

    def spy(payload):
        normalized = original(payload)
        seen.append(normalized.get("attributes"))
        return normalized

    events: list[dict] = []
    seen: list[object] = []
    original = runtime_module._policy_context
    monkeypatch.setattr(runtime_module, "_policy_context", spy)
    # A tracer instead of the redacted repr: any stringified copy in an output fails the test.
    monkeypatch.setattr(
        TrustedAttributes, "__repr__", lambda self: f"leak {self.get('customer_id')}"
    )
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-attributes-key")
    monkeypatch.setenv("SEMANTIC_RAILS_AUDIT_LOGS", "1")
    previous_resolver, previous_sink = get_policy_context_resolver(), get_audit_sink()
    set_policy_context_resolver(HostResolver())
    set_audit_sink(ListSink())
    package_path = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    app = SemanticLayerASGIApp(path=str(package_path), max_workers=1)
    try:
        yield app, seen, events
    finally:
        asyncio.run(app.aclose())
        set_policy_context_resolver(previous_resolver)
        set_audit_sink(previous_sink)


# REST route -> (MCP tool, extra MCP arguments)
_MCP = {
    "plan": ("plan", {}),
    "validate": ("execute", {"mode": "validate"}),
    "compile": ("execute", {"mode": "sql"}),
    "query": ("execute", {"mode": "run"}),
    "segment-preview": ("segment", {"action": "preview"}),
}


def _call(app, transport, operation, arguments):
    async def request():
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer test-attributes-key",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "X-Semantic-Attributes": json.dumps(SPOOF),
        }
        path, body = f"/api/v1/{operation}", arguments
        if transport == "mcp":
            tool, extra = _MCP[operation]
            params = {"name": tool, "arguments": {**arguments, **extra}}
            path, body = (
                "/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(path, json=body, headers=headers)
        payload = response.json()
        if transport == "mcp" and response.status_code == 200:
            payload = payload["result"]["structuredContent"]
        return payload

    return asyncio.run(request())


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_host_attributes_reach_the_runtime_and_never_leave_it(host_app, transport):
    app, seen, events = host_app
    spoof = {"attributes": SPOOF, "actor": "spoofed-user"}
    plan = _call(
        app,
        transport,
        "plan",
        {"intent": "revenue by store", "policy_context": spoof, "query": {"policy_context": spoof}},
    )
    assert plan["ok"] is True, plan
    query = {**plan["best"]["query_ir"], "policy_context": spoof}
    responses = [plan]
    for operation, arguments in [
        ("validate", {"query": query}),
        ("compile", {"query": query}),
        ("query", {"query": query}),
        ("segment-preview", {"segment_id": "segment.jaffle.high_value_customers"}),
    ]:
        response = _call(app, transport, operation, {**arguments, "policy_context": spoof})
        assert response["ok"] is True, response
        assert response["request_context"]["actor"] == "end-user"
        responses.append(response)
    broken = {"select": [{"expression": {"metric": "metric.missing"}}], "policy_context": spoof}
    failed = _call(app, transport, "compile", {"query": broken, "policy_context": spoof})
    assert failed["ok"] is False
    responses.append(failed)

    assert len(seen) >= 5
    assert all(attributes == HOST_ATTRIBUTES for attributes in seen)
    assert events
    assert CANARY not in json.dumps([responses, events], default=str)
