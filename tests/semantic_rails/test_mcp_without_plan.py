"""The query MCP served without plan: agents build each call from discover and inspect.

``plan=False`` on the adapter, ``SEMANTIC_RAILS_MCP_PLAN=off`` or ``--no-plan`` on
``mcp stdio`` and ``serve`` remove the plan tool and every mention of it from what the
server sends: instructions, tool descriptions, prompts, resources and recovery hints. Unset,
the server is unchanged and serves plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.api import AppState
from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.cli.app import build_parser
from semantic_rails.cli.commands import query as query_commands
from semantic_rails.cli.commands.mcp import _mcp_tool_check
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import (
    MCP_PLAN_ENV,
    MCP_SERVER_INSTRUCTIONS,
    MCP_SERVER_INSTRUCTIONS_WITHOUT_PLAN,
    SemanticLayerMCPAdapter,
    list_tool_definitions,
)
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

REPO_ROOT = Path(__file__).resolve().parents[2]
# A word starting "plan" (plan, plans, planner, Plan); response field names such as
# sql_plan don't match.
PLAN_WORD = re.compile(r"(?i)\bplan")
TOOLS_WITHOUT_PLAN = ["discover", "inspect", "valid-values", "execute", "segment"]
INSTRUCTIONS_WITHOUT_PLAN = (
    "Semantic Rails answers analytics questions from a governed semantic layer. Refer to "
    "objects by full id (measure.sales.revenue, dimension.store_name), never by label.\n"
    "\n"
    "To answer a question, build the call from ids:\n"
    "1. discover(terms) ranks measures, metrics and dimensions for it; empty terms list every "
    "id.\n"
    "2. inspect(object_id) shows one object's card: its aggregations, values, time roles and "
    "related objects. valid-values(dimension_id) lists a dimension's values to filter on.\n"
    "3. execute(query) validates, compiles and runs the call and returns at most max_rows rows "
    "(default 200); a capped result reports truncated. mode \"validate\" only checks the call; "
    'mode "sql" also returns its SQL.\n'
    "\n"
    "The call: select measure or metric ids, group_by dimension ids, where [{field, op, value}] "
    "with word ops (is, not, gt, gte, lt, lte, contains, starts_with, ends_with, missing), and "
    "time {grain, last, this, start, end}; end is exclusive. A window without a grain returns "
    "one total. Relative windows resolve against the now argument.\n"
    "No time/where: the query adds no time window or filter; metric definitions and package "
    "policies still apply.\n"
    "\n"
    "segment(segment_id, action) validates, explains or previews a package-authored segment.\n"
    "\n"
    'Every tool returns its smallest response by default (verbosity "minimal"). Responses share '
    "one character budget; omitted_fields names trimmed detail. Errors carry recovery_hints and "
    "closest_matches; follow them before retrying."
)
UNKNOWN_TOOL_HINT = (
    "Call tools/list first and pick a name from available_tools: find ids with 'discover' and "
    "'inspect', then run the call with 'execute' (mode 'validate' only checks it)."
)
REVENUE = "measure.jaffle.revenue_usd"
STORE = "dimension.jaffle_store_name"
SEGMENT = "segment.jaffle.high_value_customers"
# Calls an agent makes while building its own call, right and wrong, and the error code each
# returns (None: it succeeds).
CALLS: list[tuple[str, dict[str, Any], str | None]] = [
    ("discover", {"terms": "revenue by store"}, None),
    ("discover", {"terms": ""}, None),
    ("discover", {"term": "revenue"}, None),
    ("discover", {"terms": "weather in paris"}, None),
    ("inspect", {"object_id": REVENUE, "verbosity": "compact"}, None),
    ("inspect", {"object_id": "revenue"}, "OBJECT_NOT_FOUND"),
    ("valid-values", {"dimension_id": STORE}, None),
    ("valid-values", {"dimension_id": "store"}, "OBJECT_NOT_FOUND"),
    ("execute", {"query": {"select": [REVENUE], "group_by": [STORE]}, "verbosity": "full"}, None),
    ("execute", {"query": {"select": [REVENUE]}, "mode": "sql", "verbosity": "full"}, None),
    ("execute", {"query": {"select": [REVENUE]}, "mode": "validate"}, None),
    ("execute", {"query": {"select": ["measure.jaffle.revenu"]}}, "OBJECT_NOT_FOUND"),
    ("execute", {"query": {"select": [REVENUE], "group_by": ["dimension.nope"]}}, "OBJECT_NOT_FOUND"),
    ("execute", {"query": {"select": [REVENUE], "time": {"grain": "month"}}}, None),
    ("execute", {"query": {}}, "INVALID_QUERY"),
    ("execute", {"select": [REVENUE]}, "INVALID_QUERY"),
    ("segment", {"segment_id": SEGMENT, "action": "explain", "verbosity": "full"}, None),
    ("segment", {"segment_id": "segment.nope", "action": "validate"}, "OBJECT_NOT_FOUND"),
    ("draft", {"intent": "revenue by store"}, "UNKNOWN_MCP_TOOL"),
]


@pytest.fixture()
def runtime(runtime_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    monkeypatch.delenv(MCP_PLAN_ENV, raising=False)
    built: Runtime = runtime_factory("jaffle_shop")
    return built


@pytest.fixture()
def without_plan(runtime: Runtime) -> Iterator[SemanticLayerMCPAdapter]:
    adapter = SemanticLayerMCPAdapter(runtime, plan=False)
    try:
        yield adapter
    finally:
        adapter.close()


def _rpc(adapter: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = handle_jsonrpc_message(
        adapter, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    assert response is not None and "result" in response, response
    result: dict[str, Any] = response["result"]
    return result


def _served_surface(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    """Everything a client reads before its first call, as the transport sends it."""
    prompts = _rpc(adapter, "prompts/list")["prompts"]
    resources = _rpc(adapter, "resources/list")["resources"]
    return {
        "instructions": _rpc(adapter, "initialize")["instructions"],
        "tools": _rpc(adapter, "tools/list")["tools"],
        "prompts": prompts,
        "prompt_texts": [
            _rpc(
                adapter,
                "prompts/get",
                {"name": prompt["name"], "arguments": {"intent": "revenue", "query_json": "{}"}},
            )["messages"]
            for prompt in prompts
        ],
        "resources": resources,
        "capabilities": [
            _rpc(adapter, "resources/read", {"uri": uri})["contents"][0]["text"]
            for uri in ("semantic-rails://capabilities", "semantic-rails://capabilities/summary")
        ],
    }


def _texts(node: Any, key: str = "") -> Iterator[tuple[str, str]]:
    """Every string value in a response but an issue's ``stage``, which names an engine phase."""
    if isinstance(node, dict):
        for child_key, value in node.items():
            if child_key != "stage":
                yield from _texts(value, f"{key}.{child_key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _texts(value, f"{key}[{index}]")
    elif isinstance(node, str):
        yield key, node


def test_unset_the_server_is_unchanged_and_serves_plan(runtime: Runtime) -> None:
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        assert adapter.serves_plan is True
        contract = load_contract("query_mcp.v2.json")
        assert _rpc(adapter, "tools/list")["tools"] == contract["tools"]
        assert _rpc(adapter, "prompts/list")["prompts"] == contract["prompts"]
        assert _rpc(adapter, "initialize")["instructions"] == MCP_SERVER_INSTRUCTIONS
        # The served text names plan, so the scan below can tell the two apart.
        assert PLAN_WORD.search(json.dumps(_served_surface(adapter)))
        unknown = adapter.call_tool("draft", {})["errors"][0]["recovery_hints"][0]["message"]
        assert unknown.endswith("common entry points are 'discover', 'inspect', 'plan'.")
    finally:
        adapter.close()


def test_without_plan_the_surface_is_the_default_minus_plan(
    without_plan: SemanticLayerMCPAdapter,
) -> None:
    assert without_plan.serves_plan is False
    assert without_plan.instructions == MCP_SERVER_INSTRUCTIONS_WITHOUT_PLAN
    assert _rpc(without_plan, "initialize")["instructions"] == INSTRUCTIONS_WITHOUT_PLAN
    tools = {tool["name"]: tool for tool in _rpc(without_plan, "tools/list")["tools"]}
    assert list(tools) == TOOLS_WITHOUT_PLAN
    assert tools == {tool["name"]: tool for tool in list_tool_definitions(plan=False)}
    default = {tool["name"]: tool for tool in list_tool_definitions()}
    for name in ("discover", "inspect", "valid-values"):
        assert tools[name] == default[name], name
    execute, served = default["execute"], tools["execute"]
    assert served["description"] == execute["description"].replace(
        "Answer a call; call plan first to draft it.", "Answer a call built from discover's ids."
    )
    assert served["description"].startswith("Answer a call built from discover's ids. mode 'run'")
    verbosity = served["inputSchema"]["properties"]["verbosity"]
    assert verbosity["description"] == "'compact' adds metadata; 'full' adds explain too."
    execute["inputSchema"]["properties"]["verbosity"] = verbosity
    assert {**served, "description": ""} == {**execute, "description": ""}
    assert "Default verbosity='minimal' leaves out compiler detail; 'full' returns it." in (
        tools["segment"]["description"]
    )
    assert tools["segment"]["inputSchema"] == default["segment"]["inputSchema"]
    builder = next(
        prompt
        for prompt in _rpc(without_plan, "prompts/list")["prompts"]
        if prompt["name"] == "semantic-rails-query-builder"
    )
    assert builder["description"] == (
        "Guide an agent from a question to rows: discover, inspect (and valid-values), then "
        "execute."
    )
    text = without_plan.get_prompt(builder["name"], {"intent": "revenue"})["messages"][0]
    assert "Build the call from their ids, check filter values with valid-values" in (
        text["content"]["text"]
    )
    # The handler stays registered for hosts that read it; the tool just isn't served.
    assert "plan" in without_plan.tool_handlers


def test_without_plan_no_served_text_names_plan(without_plan: SemanticLayerMCPAdapter) -> None:
    surface = _served_surface(without_plan)
    hits = [match.group(0) for match in PLAN_WORD.finditer(json.dumps(surface))]
    assert not hits, hits
    # Only the response field sql_plan keeps the letters, as the name it has in responses.
    assert "sql_plan" in json.dumps(surface["tools"])


@pytest.mark.parametrize(("tool", "arguments", "code"), CALLS)
def test_without_plan_no_hint_or_message_names_plan(
    without_plan: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any], code: str | None
) -> None:
    response = without_plan.call_tool(tool, arguments)
    assert [issue["code"] for issue in response.get("errors") or []][:1] == ([code] if code else [])
    hits = [(key, text) for key, text in _texts(response) if PLAN_WORD.search(text)]
    assert not hits, hits


def test_calling_plan_without_it_points_at_discover_inspect_and_validate(
    without_plan: SemanticLayerMCPAdapter,
) -> None:
    response = _rpc(without_plan, "tools/call", {"name": "plan", "arguments": {"intent": "x"}})
    assert response["isError"] is True
    error = response["structuredContent"]["errors"][0]
    assert error["code"] == "UNKNOWN_MCP_TOOL"
    assert error["message"] == "Unknown MCP tool 'plan'"
    assert error["details"]["available_tools"] == sorted(TOOLS_WITHOUT_PLAN)
    [hint] = error["recovery_hints"]
    assert hint["kind"] == "use_available_tool"
    assert hint["message"] == UNKNOWN_TOOL_HINT
    assert hint["available_tools"] == sorted(TOOLS_WITHOUT_PLAN)


@pytest.mark.parametrize("plan", [True, False])
def test_an_empty_answer_points_at_the_compiled_sql(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, plan: bool
) -> None:
    adapter = SemanticLayerMCPAdapter(runtime, plan=plan)
    monkeypatch.setattr(
        adapter.runtime, "query", lambda *_args, **_kwargs: {"ok": True, "row_count": 0, "rows": []}
    )
    try:
        response = adapter.call_tool("execute", {"query": {"select": [REVENUE]}})
    finally:
        adapter.close()
    [warning] = response["warnings"]
    assert warning["code"] == "EXECUTE_EMPTY_RESULT"
    assert warning["message"].endswith(
        "Use execute with mode='sql' and verbosity='full' to inspect the "
        + ("resolved plan." if plan else "compiled SQL.")
    )


def test_every_option_source_serves_the_same_surface(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedded = SemanticLayerMCPAdapter(runtime, plan=False)
    expected = _served_surface(embedded)
    monkeypatch.setenv(MCP_PLAN_ENV, "off")
    from_env = SemanticLayerMCPAdapter(runtime)
    # The host's argument wins over the environment, either way.
    forced_on = SemanticLayerMCPAdapter(runtime, plan=True)
    monkeypatch.setenv(MCP_PLAN_ENV, "on")
    forced_off = SemanticLayerMCPAdapter(runtime, plan=False)
    served = AppState("", path=runtime.source_path, plan=False)
    monkeypatch.setenv(MCP_PLAN_ENV, " OFF ")
    asgi = SemanticLayerASGIApp(path=runtime.source_path)
    try:
        assert asgi.mcp_adapter.serves_plan is False
        for adapter in (from_env, forced_off, served.mcp_adapter, asgi.mcp_adapter):
            assert _served_surface(adapter) == expected
        assert forced_on.serves_plan is True
        assert "plan" in {tool["name"] for tool in forced_on.list_tools()}
    finally:
        for adapter in (embedded, from_env, forced_on, forced_off, served.mcp_adapter):
            adapter.close()
        asyncio.run(asgi.aclose())


@pytest.mark.parametrize("value", ["of", "disabled"])
def test_an_unknown_switch_value_is_refused(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MCP_PLAN_ENV, value)
    with pytest.raises(SemanticLayerError) as raised:
        SemanticLayerMCPAdapter(runtime)
    assert raised.value.code == "INVALID_CONFIG"
    assert str(raised.value) == f"SEMANTIC_RAILS_MCP_PLAN must be 'on' or 'off', not '{value}'."
    assert raised.value.details["valid_values"] == ["on", "off"]


def _stdio(package: Path, *, args: list[str], plan_env: str | None) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))
    env.pop(MCP_PLAN_ENV, None)
    if plan_env is not None:
        env[MCP_PLAN_ENV] = plan_env
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "plan", "arguments": {"intent": "revenue by store"}},
        },
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "semantic_rails", "mcp", "stdio", "--path", str(package), *args],
        cwd=REPO_ROOT,
        env=env,
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        timeout=60,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3], (proc.stdout, proc.stderr)
    return replies


@pytest.mark.parametrize(
    ("args", "plan_env"),
    [(["--no-plan"], None), ([], "off"), (["--no-plan"], "not-a-switch")],
    ids=["flag", "env", "flag-over-env"],
)
def test_stdio_serves_without_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], plan_env: str | None
) -> None:
    monkeypatch.delenv(MCP_PLAN_ENV, raising=False)
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    initialized, listed, called = (
        reply["result"] for reply in _stdio(package, args=args, plan_env=plan_env)
    )
    embedded = SemanticLayerMCPAdapter(Runtime.from_path(str(package)), plan=False)
    try:
        assert initialized["instructions"] == embedded.instructions
        assert listed["tools"] == embedded.list_tools()
    finally:
        embedded.close()
    assert [tool["name"] for tool in listed["tools"]] == TOOLS_WITHOUT_PLAN
    assert called["isError"] is True
    [error] = called["structuredContent"]["errors"]
    assert error["code"] == "UNKNOWN_MCP_TOOL"
    assert error["recovery_hints"][0]["message"] == UNKNOWN_TOOL_HINT


def test_stdio_refuses_an_unknown_switch_value(tmp_path: Path) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    for reply in _stdio(package, args=[], plan_env="of"):
        assert reply["error"]["data"]["code"] == "INVALID_CONFIG"
        assert reply["error"]["message"] == "SEMANTIC_RAILS_MCP_PLAN must be 'on' or 'off', not 'of'."


def test_serve_passes_no_plan_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[dict[str, Any]] = []
    monkeypatch.setattr(query_commands, "serve", lambda *args, **kwargs: served.append(kwargs))
    for argv, plan in (
        (["serve", "--package", "jaffle_shop", "--no-plan"], False),
        (["serve", "--package", "jaffle_shop"], None),
    ):
        args = build_parser().parse_args(argv)
        args.func(args)
        assert served.pop()["plan"] is plan
    stdio = build_parser().parse_args(["mcp", "stdio", "--package", "jaffle_shop", "--no-plan"])
    assert stdio.no_plan is True


@pytest.mark.parametrize("plan_env", [None, "off"])
def test_doctor_requires_plan_only_when_it_is_served(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, plan_env: str | None
) -> None:
    if plan_env is not None:
        monkeypatch.setenv(MCP_PLAN_ENV, plan_env)
    check = _mcp_tool_check(runtime)
    assert check["required_tools_present"] is True, check
    assert check["missing_required_tools"] == []
    assert check["tool_count"] == (6 if plan_env is None else 5)
