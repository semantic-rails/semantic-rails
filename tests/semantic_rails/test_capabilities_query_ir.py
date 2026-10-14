"""Capability discovery advertises only the accepted query call contract."""

from __future__ import annotations

from semantic_rails.embedding import SemanticHTTPService, SemanticLayerMCPAdapter
from semantic_rails.metadata_parts.capabilities import capabilities_payload


def test_capabilities_advertise_only_the_query_call(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        python = capabilities_payload(runtime)
        response = SemanticLayerMCPAdapter(runtime).call_tool("discover", {"terms": ""})
        assert response["ok"] is True
        resource = SemanticLayerMCPAdapter(runtime).read_resource(
            "semantic-rails://catalog/summary"
        )["payload"]["catalog"]
        http, status = SemanticHTTPService(runtime).handle("GET", "/capabilities")
        assert status == 200

        for payload in (python, response["catalog"], resource, http):
            ir_rows = [
                row
                for row in payload["capabilities"] + payload["unsupported_capabilities"]
                if row["kind"].startswith(("agent_query_ir_", "query_call_"))
            ]
            assert ir_rows == [{"kind": "query_call_v2", "available": True, "reason": ""}]
            assert payload["capabilities"] == python["capabilities"]

        # A call's version is ignored; the schema's id carries the contract's version.
        report = runtime.validate({"version": 1, "select": ["measure.jaffle.order_count"]})
        assert report["ok"] is True
    finally:
        runtime.close()
