"""Route disclosure follows caller visibility without changing route semantics."""

import json
from dataclasses import replace

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails import test_route_clarification as diamond
from tests.semantic_rails import test_route_precedence as precedence


@pytest.fixture(autouse=True)
def external_packages(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _mcp(adapter, tool, query, verbosity, audience):
    response = handle_jsonrpc_message(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "execute",
                "arguments": {
                    "query": query,
                    "verbosity": verbosity,
                    "mode": {"execute": "run", "validate": "validate", "compile": "sql"}[tool],
                },
            },
        },
        request_context=RequestContext(audience=audience),
    )
    result = response["result"]
    structured = result["structuredContent"]
    text = result["content"][0]["text"]
    assert json.loads(text) == structured
    return structured, (json.dumps(structured), text)


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("mode", ["refusal", "query_choice", "package_choice"])
@pytest.mark.parametrize("hidden", [diamond.OWNER, diamond.OWNER_ROUTE[0]])
def test_diamond_routes_in_both_mcp_channels(tmp_path, tool, verbosity, mode, hidden):
    decision = {**diamond.DIAMOND_ROW, "relationship_path": diamond.BRANCH_ROUTE}
    pkg = diamond._write_package(
        tmp_path, decisions=[decision] if mode == "package_choice" else None
    )
    config = load_package_config(str(pkg))
    config = replace(
        config,
        entities=[
            replace(entity, label="Private Owner") if entity.id == diamond.OWNER else entity
            for entity in config.entities
        ],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_owner",
                kind="object_visibility",
                action="hidden",
                object_ids=[hidden],
                audiences=["reader"],
            )
        ],
    )
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    query = dict(diamond.BALANCE_BY_DISTRICT)
    if mode == "query_choice":
        query["route_decisions"] = [decision]
    try:
        # Use the same runtime/cache across readers and authors.
        for audience in ("author", "reader", "author"):
            out, channels = _mcp(adapter, tool, query, verbosity, audience)
            assert out["ok"] is (mode != "refusal")
            if mode == "refusal":
                assert out["errors"][0]["code"] == "AMBIGUOUS_PATH"
            elif tool == "execute":
                assert diamond._rows(out, ["dimension.bank_district_name", "v"]) == diamond._gold(
                    diamond.BY_BRANCH
                )
            for channel in channels:
                if audience == "reader":
                    for token in ("Private Owner", diamond.OWNER, *diamond.OWNER_ROUTE):
                        assert token not in channel
                elif mode == "refusal" or (mode == "query_choice" and verbosity != "minimal"):
                    assert diamond.OWNER_ROUTE[0] in channel
            if mode != "refusal":
                direct = getattr(runtime, {"execute": "query"}.get(tool, tool))(
                    {
                        **query,
                        "verbosity": verbosity,
                        "policy_context": RequestContext(audience=audience).to_policy_context(),
                    }
                )
                if audience == "reader":
                    assert all(
                        token not in json.dumps(direct)
                        for token in ("Private Owner", diamond.OWNER, *diamond.OWNER_ROUTE)
                    )
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_successful_route_projection_withholds_ids_when_visibility_fails(
    tmp_path, monkeypatch, tool, verbosity
):
    decision = {**diamond.DIAMOND_ROW, "relationship_path": diamond.BRANCH_ROUTE}
    pkg = diamond._write_package(tmp_path, decisions=[decision])
    config = replace(
        load_package_config(str(pkg)),
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_owner",
                kind="object_visibility",
                action="hidden",
                object_ids=[diamond.OWNER],
            )
        ],
    )
    monkeypatch.setattr("semantic_rails.runtime.diagnostic_hidden_object_ids", lambda *_: None)
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out, channels = _mcp(adapter, tool, diamond.BALANCE_BY_DISTRICT, verbosity, "reader")
        assert out["ok"] is True
        direct = getattr(runtime, {"execute": "query"}.get(tool, tool))(
            {**diamond.BALANCE_BY_DISTRICT, "verbosity": verbosity}
        )
        for channel in (*channels, json.dumps(direct)):
            assert "relationship." not in channel
            assert "ROUTE_RECORDED" not in channel
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("unknown", [False, True])
def test_no_visible_refusal_option_still_refuses(tmp_path, monkeypatch, tool, verbosity, unknown):
    pkg = diamond._write_package(tmp_path)
    config = replace(
        load_package_config(str(pkg)),
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_routes",
                kind="object_visibility",
                action="hidden",
                object_ids=[diamond.OWNER, "entity.bank_branch"],
            )
        ],
    )
    if unknown:
        monkeypatch.setattr("semantic_rails.runtime.diagnostic_hidden_object_ids", lambda *_: None)
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out, channels = _mcp(adapter, tool, diamond.BALANCE_BY_DISTRICT, verbosity, "reader")
        assert out["ok"] is False
        assert out["errors"][0]["code"] == "AMBIGUOUS_PATH"
        for channel in channels:
            assert "admin" in channel
            for token in (*diamond.OWNER_ROUTE, *diamond.BRANCH_ROUTE, "Owner", "Branch"):
                assert token not in channel
        assert out["errors"][0]["details"]["clarification"]["options"] == []
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("mode", ["conflict", "inherited"])
def test_related_route_rows_are_visible_before_they_are_named(tmp_path, tool, verbosity, mode):
    # A visible own-key route conflicts with a recorded route through a hidden client.
    # An inherited visible route can instead follow a row whose other endpoint is hidden.
    hidden = precedence._entity("client")
    rows = (
        [precedence.ACCOUNT_OWNER_ROW]
        if mode == "conflict"
        else [
            precedence._row(hidden, precedence.DISTRICT, [precedence.OWNER[0], *precedence.BRANCH])
        ]
    )
    pkg = precedence._write_package(
        tmp_path,
        relationships=precedence.OWN_DISTRICT if mode == "conflict" else precedence.LENDER,
        rows=rows,
    )
    config = replace(
        load_package_config(str(pkg)),
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_waypoint",
                kind="object_visibility",
                action="hidden",
                object_ids=[hidden],
                audiences=["reader"],
            )
        ],
    )
    query = precedence._query(
        precedence.LOAN_AMOUNT if mode == "conflict" else precedence.BALANCE,
        group_by=[precedence.DISTRICT_NAME],
    )
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        for audience in ("author", "reader"):
            out, channels = _mcp(adapter, tool, query, verbosity, audience)
            assert out["ok"] is True, out
            if tool == "execute":
                gold = (
                    precedence.OWN_KEY_GOLD
                    if mode == "conflict"
                    else precedence._by_account_route("account", "branch")
                )
                assert precedence._rows(out, [precedence.DISTRICT_NAME, "v"]) == precedence._gold(
                    gold
                )
            if verbosity != "minimal":
                for channel in channels:
                    token = precedence.OWNER[0] if mode == "conflict" else hidden
                    assert (token in channel) is (audience == "author")
                    if audience == "reader" and mode == "conflict":
                        assert all(rel not in channel for rel in precedence.OWNER)
    finally:
        adapter.close()
        runtime.close()
