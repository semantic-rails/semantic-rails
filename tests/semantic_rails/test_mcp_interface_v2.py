"""Query MCP interface v2, the only interface since v1 was removed.

v2 folded v1's validate and compile into ``execute(mode)`` and its three segment
tools into ``segment(action)``, dropped capabilities, catalog and build-options,
and defaults every tool to its smallest response.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.cli.commands.mcp import _mcp_tool_check
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import (
    MCP_DEFAULT_MAX_ROWS,
    MCP_SERVER_INSTRUCTIONS,
    SemanticLayerMCPAdapter,
)
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.metadata_parts.relevance import _no_viable_candidates_block
from semantic_rails.planner.plan import _trim_why_errors
from semantic_rails.request_context import RequestContext

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
SEGMENT = "segment.jaffle.high_value_customers"
QUERY = {
    "version": 2,
    "select": [{"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
    "group_by": [STORE],
    "time": {"temporal_role": ORDER_TIME, "grain": "month", "start": "2017-01-01"},
    "order_by": [{"field": "time", "direction": "ASC"}, {"field": STORE, "direction": "ASC"}],
}
# A window without a grain groups by the raw timestamp: thousands of rows.
UNGRAINED = {
    **QUERY,
    "time": {"temporal_role": ORDER_TIME, "start": "2017-01-01"},
    "order_by": [{"field": STORE, "direction": "ASC"}],
}
V2_TOOLS = ["discover", "inspect", "valid-values", "plan", "execute", "segment"]
V1_ONLY_TOOLS = {
    "capabilities",
    "catalog",
    "build-options",
    "validate",
    "compile",
    "segment-validate",
    "segment-explain",
    "segment-preview",
}
VOLATILE = {"request_id", "timing_ms", "api_version", "request_context"}


@pytest.fixture()
def runtime(runtime_factory: Any) -> Iterator[Any]:
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


@pytest.fixture()
def v2(runtime: Any) -> SemanticLayerMCPAdapter:
    return SemanticLayerMCPAdapter(runtime)


def _stable(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key not in VOLATILE}


def _initialize(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = handle_jsonrpc_message(adapter, message)
    assert response is not None
    result: dict[str, Any] = response["result"]
    return result


def test_asking_for_the_removed_v1_interface_fails(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = "SEMANTIC_RAILS_MCP_INTERFACE"
    monkeypatch.setenv(env, " V2 ")
    assert SemanticLayerMCPAdapter(runtime).interface == "v2"
    for argument, environment in (("v1", ""), (None, "v1"), (None, "v3"), ("", "v1")):
        monkeypatch.setenv(env, environment)
        with pytest.raises(SemanticLayerError) as raised:
            SemanticLayerMCPAdapter(runtime, interface=argument)
        assert raised.value.code == "INVALID_CONFIG"
        assert "v1 MCP interface was removed; v2 is the only interface" in str(raised.value)
    # mcp stdio, http and doctor build their adapter from the environment.
    with pytest.raises(SemanticLayerError, match="was removed; v2 is the only interface"):
        _mcp_tool_check(runtime)


def test_a_stdio_client_pinned_to_v1_gets_the_refusal(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a closed connection: the refusal answers initialize and goes to stderr."""

    from semantic_rails.cli.commands import mcp as mcp_commands

    monkeypatch.setenv("SEMANTIC_RAILS_MCP_INTERFACE", "v1")
    monkeypatch.setattr(mcp_commands, "_runtime_from_package_or_path", lambda _args: runtime)
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"{json.dumps(initialize)}\n{json.dumps(initialized)}\n")
    )
    with pytest.raises(SystemExit):
        mcp_commands.cmd_mcp_stdio(argparse.Namespace())
    out, err = capsys.readouterr()
    [reply] = [json.loads(line) for line in out.splitlines()]
    assert reply["id"] == 1
    assert reply["error"]["data"] == {"code": "INVALID_CONFIG"}
    assert "v1 MCP interface was removed" in reply["error"]["message"]
    assert "v1 MCP interface was removed" in err


def test_the_adapter_serves_the_frozen_contract(v2: SemanticLayerMCPAdapter) -> None:
    manifest = load_contract("query_mcp.v2.json")
    assert v2.list_tools() == manifest["tools"]
    assert v2.list_resources() == manifest["resources"]
    assert v2.list_prompts() == manifest["prompts"]
    assert manifest["interface_version"] == v2.interface == "v2"
    assert [tool["name"] for tool in v2.list_tools()] == V2_TOOLS
    initialized = _initialize(v2)
    assert initialized["serverInfo"]["version"] == "v2"
    assert initialized["instructions"] == MCP_SERVER_INSTRUCTIONS
    assert len(MCP_SERVER_INSTRUCTIONS) <= 2048


def test_tools_default_to_small_responses_and_state_the_workflow() -> None:
    tools = {tool["name"]: tool for tool in load_contract("query_mcp.v2.json")["tools"]}
    assert tools["plan"]["inputSchema"]["properties"]["detail"]["default"] == "query"
    execute = tools["execute"]["inputSchema"]["properties"]
    assert execute["max_rows"]["default"] == MCP_DEFAULT_MAX_ROWS
    assert execute["mode"]["enum"] == ["run", "validate", "sql"]
    assert "exclusive" in execute["query"]["properties"]["time"]["properties"]["end"]["description"]
    for tool in ("discover", "inspect"):
        assert tools[tool]["inputSchema"]["properties"]["verbosity"]["default"] == "minimal"
    # Hosts that drop the server instructions still see plan-first and the exclusive end.
    assert "call it before 'execute'" in tools["plan"]["description"]
    assert "call plan first" in tools["execute"]["description"]
    assert "time.end is exclusive" in tools["execute"]["description"]


def _descriptions(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _descriptions(item)


def test_v2_text_names_only_v2_tools(v2: SemanticLayerMCPAdapter) -> None:
    texts = [MCP_SERVER_INSTRUCTIONS]
    texts += _descriptions([v2.list_tools(), v2.list_prompts(), v2.list_resources()])
    for prompt, arguments in (
        ("semantic-rails-query-builder", {"intent": "revenue by store"}),
        ("semantic-rails-query-review", {"query_json": "{}"}),
        ("semantic-rails-segment-workflow", {"segment_id": SEGMENT}),
    ):
        texts.append(v2.get_prompt(prompt, arguments)["messages"][0]["content"]["text"])
    for text in texts:
        for name in V1_ONLY_TOOLS:
            assert f"'{name}' tool" not in text and f"{name}(" not in text, (name, text)
        for name in V1_ONLY_TOOLS - {"validate"}:
            assert f"'{name}'" not in text and f"`{name}`" not in text, (name, text)


def test_execute_modes_run_validate_or_compile(v2: SemanticLayerMCPAdapter) -> None:
    for arguments, sql, rows in (
        ({"query": QUERY}, False, True),
        ({"query": QUERY, "mode": "validate"}, False, False),
        ({"query": QUERY, "mode": "sql", "row_format": "columns"}, True, False),
        # Top-level Query IR passthrough works in every mode.
        ({**QUERY, "mode": "validate"}, False, False),
    ):
        response = v2.call_tool("execute", arguments)
        assert response["ok"] is True, response["errors"]
        assert response["api_version"] == "v2"
        assert ("rendered_sql" in response, "rows" in response) == (sql, rows), arguments


def test_execute_caps_rows_by_default(v2: SemanticLayerMCPAdapter) -> None:
    capped = v2.call_tool("execute", {"query": UNGRAINED})
    assert capped["truncated"] is True
    assert capped["row_count"] == MCP_DEFAULT_MAX_ROWS
    assert "EXECUTE_ROWS_TRUNCATED" in {warning["code"] for warning in capped["warnings"]}
    assert v2.call_tool("execute", {"query": UNGRAINED, "max_rows": 5})["row_count"] == 5


def test_bad_modes_and_actions_are_argument_errors(v2: SemanticLayerMCPAdapter) -> None:
    for tool, arguments, field in (
        ("execute", {"query": QUERY, "mode": "explain"}, "mode"),
        ("segment", {"segment_id": SEGMENT, "action": "run"}, "action"),
        ("segment", {"segment_id": SEGMENT}, "action"),
        ("segment", {"segment_id": SEGMENT, "action": "validate", "mode": "x"}, None),
    ):
        response = v2.call_tool(tool, arguments)
        error = response["errors"][0]
        assert response["ok"] is False and error["code"] == "INVALID_MCP_ARGUMENTS"
        if field:
            assert error["details"]["field"] == field


@pytest.mark.parametrize("action", ["validate", "explain", "preview"])
def test_segment_actions_default_to_minimal_verbosity(
    v2: SemanticLayerMCPAdapter, action: str
) -> None:
    # limit only applies to previews, and is accepted with any action.
    arguments = {"segment_id": SEGMENT, "limit": 3, "action": action}
    default = v2.call_tool("segment", arguments)
    minimal = v2.call_tool("segment", {**arguments, "verbosity": "minimal"})
    full = v2.call_tool("segment", {**arguments, "verbosity": "full"})
    assert default["ok"] is True, default["errors"]
    assert set(default) == set(minimal) and len(str(default)) < len(str(full))
    for key in ("status", "member_count", "preview_row_count", "derived_query"):
        assert default.get(key) == full.get(key), key


def test_plan_defaults_to_the_compact_query_detail(v2: SemanticLayerMCPAdapter) -> None:
    intent = {"intent": "revenue by store"}
    compact = _stable(v2.call_tool("plan", intent))
    assert compact == _stable(v2.call_tool("plan", {**intent, "detail": "query"}))
    assert "intent_ir" not in compact
    assert "intent_ir" in v2.call_tool("plan", {**intent, "detail": "best"})


def test_discover_and_inspect_return_slim_cards_by_default(v2: SemanticLayerMCPAdapter) -> None:
    for tool, arguments in (
        ("discover", {"terms": "revenue by store"}),
        ("inspect", {"object_id": "measure.jaffle.revenue_usd"}),
    ):
        slim = _stable(v2.call_tool(tool, arguments))
        assert slim == _stable(v2.call_tool(tool, {**arguments, "verbosity": "minimal"}))
        full = v2.call_tool(tool, {**arguments, "verbosity": "compact"})
        assert len(str(slim)) < len(str(full))


def test_discover_with_empty_terms_lists_ids(v2: SemanticLayerMCPAdapter) -> None:
    # A page large enough for every kind, whatever the fixture's size.
    listed = v2.call_tool("discover", {"terms": "", "limit": 10_000})
    assert listed["ok"] is True and listed["warnings"] == []
    index = v2.read_resource("semantic-rails://catalog/index")["payload"]["catalog"]
    assert listed["catalog"] == index
    segments = v2.call_tool("discover", {"terms": " ", "kinds": ["segment"]})["catalog"]
    assert SEGMENT in segments["segment_ids"] and "measure_ids" not in segments
    two = v2.call_tool("discover", {"terms": "", "kinds": "segment,metric"})["catalog"]
    assert {key for key in two if key.endswith("_ids")} == {"segment_ids", "metric_ids"}


def test_empty_terms_discover_pages_ids_per_kind(v2: SemanticLayerMCPAdapter) -> None:
    whole = v2.call_tool("discover", {"terms": "", "limit": 10_000})["catalog"]
    first = v2.call_tool("discover", {"terms": "", "limit": 5})
    warning = first["warnings"][0]
    assert warning["code"] == "DISCOVER_IDS_TRUNCATED"
    assert warning["details"]["next_offset"] == 5
    assert warning["details"]["remaining"]["dimension"] == len(whole["dimension_ids"]) - 5
    assert first["catalog"]["counts"] == whole["counts"]
    assert first["catalog"]["dimension_ids"] == whole["dimension_ids"][:5]
    second = v2.call_tool("discover", {"terms": "", "limit": 5, "offset": 5})["catalog"]
    assert second["dimension_ids"] == whole["dimension_ids"][5:10]
    # The last page of one kind lists what's left and warns about nothing.
    rest = {"terms": "", "kinds": ["dimension"], "limit": 60, "offset": 60}
    last = v2.call_tool("discover", rest)
    assert last["catalog"]["dimension_ids"] == whole["dimension_ids"][60:]
    assert last["warnings"] == [] and "measure_ids" not in last["catalog"]


def test_unknown_tool_hint_names_only_tools_the_interface_has(
    v2: SemanticLayerMCPAdapter,
) -> None:
    hint = v2.call_tool("forecast", {})["errors"][0]["recovery_hints"][0]["message"]
    assert hint.endswith("common entry points are 'discover', 'inspect', 'plan'.")


AIRSPEED = {"measure": "measure.jaffle.airspeed", "aggregation": "sum"}


def _hint_texts(node: Any, key: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        for child_key, value in node.items():
            yield from _hint_texts(value, child_key)
    elif isinstance(node, list):
        for item in node:
            yield from _hint_texts(item, key)
    elif isinstance(node, str) and key in {"recovery_hint", "message", "hint"}:
        yield node


def _sends_to_removed_surface(text: str) -> bool:
    """Whether ``text`` points an MCP agent at a removed v1 tool or an HTTP route."""

    # "execute with mode 'validate'" and "segment with action 'preview'" are v2 calls.
    text = re.sub(r"(mode|action) '[a-z]+'", "", text.lower())
    names = "|".join(re.escape(name) for name in V1_ONLY_TOOLS)
    return "/api/v1/" in text or bool(
        re.search(rf"[`']({names})[`']|\b({names}) tool\b|\bor ({names}) to\b", text)
        or re.search(rf"\b(call|use|run|try)\s+(the\s+)?({names})\b", text)
    )


def test_the_removed_surface_matcher_catches_the_old_wording() -> None:
    for old in (
        "Use compose_hints to author a Query IR directly, then call validate.",
        "Call validate on best.query_ir to see why the semantically closest draft failed.",
        "+4 additional validation errors; call validate on best.query_ir for the full list.",
        "Use /api/v1/discover to find what IS available.",
    ):
        assert _sends_to_removed_surface(old), old
    assert not _sends_to_removed_surface(
        "then validate it (over MCP, execute with mode 'validate')"
    )


def _unrealizable(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan drafts nothing, so it answers NO_PATTERN_MATCH."""

    from semantic_rails.planner import plan as plan_module
    from semantic_rails.planner.orchestrator import CompositionResult

    def compose(runtime: Any, intent: str) -> CompositionResult:
        return CompositionResult(intent_ir=plan_module.parse_intent(runtime, intent), draft=None)

    monkeypatch.setattr(plan_module, "compose", compose)
    monkeypatch.setattr(plan_module, "_distinct_fallback_drafts", lambda *args, **kwargs: [])


@pytest.mark.parametrize(
    ("tool", "arguments", "reached"),
    [
        ("discover", {"terms": "unladen swallow"}, "discover_low_relevance"),
        ("plan", {"intent": "unladen swallow airspeed", "detail": "best"}, "LOW_RELEVANCE"),
        # A drafted fallback that failed validation: status low_confidence at the default detail.
        ("plan", {"intent": "orders by customer status"}, "PLAN_FALLBACK_SEMANTIC_DRIFT"),
        ("plan", {"intent": "revenue by store"}, "NO_PATTERN_MATCH"),
        ("inspect", {"object_id": "measure.jaffle.airspeed"}, "call_discover_to_locate"),
        (
            "execute",
            {"query": {**QUERY, "select": [{"as": "x", "expression": AIRSPEED}]}},
            "call_discover_to_locate",
        ),
    ],
)
def test_recovery_hints_never_send_the_agent_to_a_removed_tool(
    v2: SemanticLayerMCPAdapter,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
    reached: str,
) -> None:
    if reached == "NO_PATTERN_MATCH":
        _unrealizable(monkeypatch)
    response = v2.call_tool(tool, arguments)
    assert f'"{reached}"' in json.dumps(response), "the call should reach the hint under test"
    texts = list(_hint_texts(response))
    assert texts, "the call should come back with recovery guidance"
    assert not [text for text in texts if _sends_to_removed_surface(text)]


def test_hints_built_outside_those_calls_point_at_v2_calls() -> None:
    why = _trim_why_errors([{"code": "INVALID_QUERY", "message": "bad"}] * 4)
    assert why["truncated"]["dropped"] == 1
    blocked = _no_viable_candidates_block("x", blocked_codes=[], sample_blocked_messages=[])
    texts = [*_hint_texts(why), blocked["recovery_hint"]]
    assert not [text for text in texts if _sends_to_removed_surface(text)]


@pytest.mark.parametrize("tool", sorted(V1_ONLY_TOOLS))
def test_removed_v1_tools_point_to_their_replacement(
    v2: SemanticLayerMCPAdapter, tool: str
) -> None:
    response = v2.call_tool(tool, {})
    error = response["errors"][0]
    assert error["code"] == "UNKNOWN_MCP_TOOL"
    assert error["details"]["available_tools"] == sorted(V2_TOOLS)
    replacement = error["details"]["replacement"]
    assert (
        error["message"]
        == f"The '{tool}' tool was removed with MCP interface v1; use {replacement}."
    )
    assert error["recovery_hints"][0]["message"] == f"Use {replacement} instead."
    assert not _sends_to_removed_surface(replacement)


def test_mcp_doctor_checks_the_v2_tools(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEMANTIC_RAILS_MCP_INTERFACE", raising=False)
    check = _mcp_tool_check(runtime)
    assert check["interface"] == "v2"
    assert check["required_tools_present"] is True
    assert check["tool_count"] == len(V2_TOOLS)


def test_v2_tools_take_the_trusted_transport_context(v2: SemanticLayerMCPAdapter) -> None:
    for tool in v2.list_tools():
        assert {"request_id", "policy_context"} <= set(tool["inputSchema"]["properties"])
    context = RequestContext(
        request_id="trusted", actor="analyst@example.com", roles=("analyst",), environment="dev"
    )
    seen: dict[str, Any] = {}

    def validate(arguments: dict[str, Any]) -> dict[str, Any]:
        seen.update(arguments)
        return {"ok": True}

    v2._handle_validate = validate  # type: ignore[method-assign]
    claimed = {"roles": ["admin"], "environment": "prod"}
    response = v2.call_tool(
        "execute",
        {"query": {**QUERY, "policy_context": claimed}, "mode": "validate", "request_id": "x"},
        request_context=context,
    )
    assert response["request_id"] == "trusted"
    assert seen["policy_context"] == context.to_policy_context()
    assert "policy_context" not in seen["query"] and "mode" not in seen


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("discover", {"terms": "revenue"}),
        ("discover", {"terms": ""}),
        ("inspect", {"object_id": "measure.jaffle.revenue_usd"}),
        ("valid-values", {"dimension_id": STORE}),
        ("plan", {"intent": "revenue by store"}),
        ("execute", {"query": QUERY}),
        ("execute", {"query": QUERY, "mode": "validate"}),
        ("execute", {"query": QUERY, "mode": "sql"}),
        ("segment", {"segment_id": SEGMENT, "action": "validate"}),
        ("segment", {"segment_id": SEGMENT, "action": "explain"}),
        ("segment", {"segment_id": SEGMENT, "action": "preview", "limit": 2}),
    ],
)
def test_every_v2_call_works_behind_an_authenticated_transport(
    v2: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    context = RequestContext(
        request_id="trusted", actor="analyst@example.com", roles=("analyst",), environment="dev"
    )
    claimed = {**arguments, "request_id": "caller", "policy_context": {"roles": ["admin"]}}
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": claimed},
    }
    response = handle_jsonrpc_message(v2, message, request_context=context)
    assert response is not None
    result = response["result"]
    payload = result["structuredContent"]
    assert result["isError"] is False and payload["ok"] is True, payload.get("errors")
    assert payload["request_id"] == "trusted"
    assert payload["request_context"]["roles"] == ["analyst"]
    assert not [w for w in payload["warnings"] if str(w.get("code", "")).endswith("_UNKNOWN_ARG")]


def test_envelopes_state_each_fact_once(v2: SemanticLayerMCPAdapter) -> None:
    ok = v2.call_tool("plan", {"intent": "revenue by store"})
    assert "request_context" not in ok and "recovery_hints" not in ok
    assert {"ok", "status", "api_version", "request_id", "package_id", "warnings", "errors"} <= set(
        ok
    )
    # A select item without its "expression" wrapper names the shape to use.
    bad = {"version": 2, "select": [{"metric": "metric.sales.aov_usd", "as": "aov"}]}
    failed = v2.call_tool("execute", {"query": bad})
    issue = failed["errors"][0]
    assert failed["error"] == issue
    assert issue["code"] == "INVALID_EXPRESSION_AST"
    assert "under 'expression'" in issue["message"] and "['as', 'metric']" in issue["message"]
    assert failed["recovery_hints"] == issue["recovery_hints"]
    empty = [key for key, value in issue.items() if value in (None, "", [], {})]
    assert empty == [] and "why_invalid" not in issue and "unsupported_construct" not in issue


def test_lean_issues_drop_only_empty_fields_and_echoes() -> None:
    from semantic_rails.mcp import _lean_issue

    details = {"path": "select[0]"}
    issue = {
        "code": "C",
        "message": "m",
        "why_invalid": "m",
        "unsupported_construct": "C",
        "details": details,
        "path": "",
        "object_ids": [],
        "recovery_hints": [{"kind": "k", "message": "h", "details": details, "shape": {}}],
    }
    assert _lean_issue(issue) == {
        "code": "C",
        "message": "m",
        "details": details,
        "recovery_hints": [{"kind": "k", "message": "h"}],
    }
    kept = {"code": "C", "message": "m", "why_invalid": "w", "unsupported_construct": "U"}
    assert _lean_issue(kept) == kept
