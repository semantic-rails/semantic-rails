"""Capability discovery advertises only the accepted query call contract."""

from __future__ import annotations

import pytest

from semantic_rails.embedding import SemanticHTTPService, SemanticLayerMCPAdapter
from semantic_rails.metadata_parts.capabilities import capabilities_payload


@pytest.mark.parametrize("surface", ["python", "mcp-tool", "mcp-resource", "http"])
def test_capabilities_advertise_only_the_query_call(runtime_factory, surface):
    runtime = runtime_factory("jaffle_shop")
    try:
        if surface == "python":
            payload = capabilities_payload(runtime)
        elif surface == "mcp-tool":
            response = SemanticLayerMCPAdapter(runtime).call_tool("discover", {"terms": ""})
            assert response["ok"] is True
            payload = response["catalog"]
        elif surface == "mcp-resource":
            payload = SemanticLayerMCPAdapter(runtime).read_resource(
                "semantic-rails://catalog/summary"
            )["payload"]["catalog"]
        else:
            payload, status = SemanticHTTPService(runtime).handle("GET", "/capabilities")
            assert status == 200

        ir_rows = [
            row
            for row in payload["capabilities"] + payload["unsupported_capabilities"]
            if row["kind"].startswith(("agent_query_ir_", "query_call_"))
        ]
        assert ir_rows == [{"kind": "query_call_v2", "available": True, "reason": ""}]
        assert capabilities_payload(runtime)["capabilities"] == payload["capabilities"]

        # A call's version is ignored; the schema's id carries the contract's version.
        report = runtime.validate({"version": 1, "select": ["measure.jaffle.order_count"]})
        assert report["ok"] is True
    finally:
        runtime.close()
