"""A host can serve the query MCP without ``plan`` (``SEMANTIC_RAILS_MCP_HIDE_PLAN``).

With it set, plan is neither listed nor callable, and nothing the server sends names it: the
instructions, tool descriptions, prompts, capabilities and error hints describe discover ->
inspect -> execute (validate) -> execute. Unset, the server sends exactly what it sent before.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.cli.commands.mcp import _mcp_tool_check
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import (
    _PROMPT_TEXT,
    HIDE_PLAN_ENV,
    MCP_SERVER_INSTRUCTIONS,
    SemanticLayerMCPAdapter,
    list_prompt_definitions,
    list_tool_definitions,
)
from semantic_rails.mcp_server import handle_jsonrpc_message

PLAN = re.compile(r"\bplan\b", re.IGNORECASE)
# sha256 of the default surface as it was before the option existed.
DEFAULT_SURFACE_SHA256 = {
    "instructions": "2ef8432647e908ff17b12d32cef623189a35fdb39e0be2a69c272e73773e92b8",
    "tools": "9126d52c0d3042edc2ddb863066f46a5a72553366276e13e4442e136f2d6c8a4",
    "prompts": "bb6cabd6d00e3b2b7f8367522fdfd3dcf4efe0d0c04cf999dd1436053fd4cdae",
    "prompt_text": "6458d5136efb7a93d1fc66c2486627688010b38939af5ec85a8555f9cabcd6f0",
}


def _sha(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture()
def served(runtime_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.delenv(HIDE_PLAN_ENV, raising=False)
    adapters: list[SemanticLayerMCPAdapter] = []

    def build(hide_plan: bool | None = None) -> SemanticLayerMCPAdapter:
        adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"), hide_plan=hide_plan)
        adapters.append(adapter)
        return adapter

    yield build
    for adapter in adapters:
        adapter.close()


def _rpc(adapter: SemanticLayerMCPAdapter, method: str, params: Any = None) -> dict[str, Any]:
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    response = handle_jsonrpc_message(adapter, message)
    assert isinstance(response, dict) and "result" in response, response
    result: dict[str, Any] = response["result"]
    return result


def test_default_surface_is_unchanged(served: Any) -> None:
    assert _sha(MCP_SERVER_INSTRUCTIONS) == DEFAULT_SURFACE_SHA256["instructions"]
    assert _sha(list_tool_definitions()) == DEFAULT_SURFACE_SHA256["tools"]
    assert _sha(list_prompt_definitions()) == DEFAULT_SURFACE_SHA256["prompts"]
    assert _sha(_PROMPT_TEXT) == DEFAULT_SURFACE_SHA256["prompt_text"]
    adapter = served()
    assert adapter.hide_plan is False
    assert _rpc(adapter, "initialize")["instructions"] == MCP_SERVER_INSTRUCTIONS
    assert _rpc(adapter, "tools/list")["tools"] == list_tool_definitions()
    assert _rpc(adapter, "prompts/list")["prompts"] == list_prompt_definitions()
    assert adapter.call_tool("plan", {"intent": "revenue by store"})["ok"] is True
    hint = adapter.call_tool("plann", {})["errors"][0]["recovery_hints"][0]
    assert hint["message"] == (
        "Call tools/list first and pick a name from available_tools — common entry points "
        "are 'discover', 'inspect', 'plan'."
    )


def test_hidden_plan_is_neither_listed_nor_callable(served: Any) -> None:
    adapter = served(hide_plan=True)
    names = [tool["name"] for tool in _rpc(adapter, "tools/list")["tools"]]
    assert names == [tool["name"] for tool in list_tool_definitions() if tool["name"] != "plan"]
    assert "plan" not in adapter.tool_handlers
    with pytest.raises(ValueError, match="Unknown MCP tool 'plan'"):
        adapter.replace_tool_handler("plan", lambda args: {})
    response = adapter.call_tool("plan", {"intent": "revenue by store"})
    assert response["ok"] is False
    assert response["error"] == {"code": "UNKNOWN_MCP_TOOL", "message": "Unknown MCP tool 'plan'"}
    issue = response["errors"][0]
    assert "plan" not in issue["details"]["available_tools"]
    for hint in issue["recovery_hints"]:
        assert not PLAN.search(hint["message"]), hint
        assert "plan" not in hint["available_tools"]
    assert "closest_matches" not in json.dumps(response)


def test_hidden_mode_never_names_plan(served: Any) -> None:
    adapter = served(hide_plan=True)
    instructions = _rpc(adapter, "initialize")["instructions"]
    assert 'execute(query, mode "validate")' in instructions
    assert len(instructions) < len(MCP_SERVER_INSTRUCTIONS)
    # The written call is validated first, so execute no longer says to skip validate.
    execute = next(tool for tool in adapter.list_tools() if tool["name"] == "execute")
    assert "skip it" not in execute["description"]
    assert "check a call you wrote before running it" in execute["description"]
    sent: list[Any] = [
        instructions,
        _rpc(adapter, "tools/list"),
        prompts := _rpc(adapter, "prompts/list"),
    ]
    for prompt in prompts["prompts"]:
        arguments = {arg["name"]: "x" for arg in prompt["arguments"]}
        sent.append(_rpc(adapter, "prompts/get", {"name": prompt["name"], "arguments": arguments}))
    for uri in ("semantic-rails://capabilities", "semantic-rails://capabilities/summary"):
        sent.append(_rpc(adapter, "resources/read", {"uri": uri}))
    # Errors and their recovery hints and closest matches on the calls an agent writes itself.
    for name, arguments in (
        ("execute", {"query": {"select": ["measure.jaffle.revenue_us"]}, "mode": "validate"}),
        ("execute", {"select": ["measure.jaffle.revenue_usd"]}),
        ("execute", {"query": {"select": ["measure.jaffle.revenue_usd"]}, "mode": "validat"}),
        ("inspect", {"object_id": "revenue"}),
        ("discover", {"term": "revenue"}),
        ("prompt", {}),
    ):
        response = adapter.call_tool(name, arguments)
        assert response["ok"] is False or response.get("warnings"), (name, response)
        sent.append(response)
    for payload in sent:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        assert not PLAN.search(text), PLAN.findall(text)


@pytest.mark.parametrize(
    ("value", "hidden"),
    [("1", True), (" TRUE ", True), ("on", True), ("", False), ("0", False), ("off", False)],
)
def test_the_environment_sets_the_option(
    served: Any, monkeypatch: pytest.MonkeyPatch, value: str, hidden: bool
) -> None:
    monkeypatch.setenv(HIDE_PLAN_ENV, value)
    adapter = served()
    assert adapter.hide_plan is hidden
    assert ("plan" in {tool["name"] for tool in adapter.list_tools()}) is not hidden
    mcp = _mcp_tool_check(adapter.runtime)
    assert mcp["required_tools_present"] is True, mcp
    # The constructor argument wins over the environment.
    assert served(hide_plan=not hidden).hide_plan is not hidden


def test_an_unknown_setting_refuses(served: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HIDE_PLAN_ENV, "plan")
    with pytest.raises(SemanticLayerError) as caught:
        served()
    assert caught.value.code == "INVALID_CONFIG"
    assert str(caught.value) == (
        "SEMANTIC_RAILS_MCP_HIDE_PLAN must be one of 1, true, yes, on, 0, false, no, off, or unset."
    )
    assert caught.value.details == {
        "setting": HIDE_PLAN_ENV,
        "value": "plan",
        "valid_values": ["1", "true", "yes", "on", "0", "false", "no", "off"],
    }
