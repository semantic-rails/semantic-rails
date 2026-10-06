"""Capability discovery advertises only the accepted Query IR contract."""

from __future__ import annotations

import pytest

from semantic_rails.embedding import SemanticHTTPService, SemanticLayerMCPAdapter
from semantic_rails.metadata_parts.capabilities import capabilities_payload


@pytest.mark.parametrize("surface", ["python", "mcp-tool", "mcp-resource", "http"])
def test_capabilities_advertise_only_query_ir_v1(runtime_factory, surface):
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
            if row["kind"].startswith("agent_query_ir_")
        ]
        assert ir_rows == [{"kind": "agent_query_ir_v1", "available": True, "reason": ""}]
        assert capabilities_payload(runtime)["capabilities"] == payload["capabilities"]

        report = runtime.validate(
            {
                "version": 2,
                "select": [{"expression": {"measure": "measure.jaffle.order_count"}}],
            }
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_QUERY"
        assert report["errors"][0]["details"]["supported_versions"] == [1]
    finally:
        runtime.close()
