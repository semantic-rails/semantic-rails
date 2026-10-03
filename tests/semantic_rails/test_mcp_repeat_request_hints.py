"""Session hints refer to earlier calls without changing execution or first responses."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest

from scripts.mcp_context import V2_PROBES, normalize_volatile
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text
from semantic_rails.mcp_server import serve_stdio
from semantic_rails.mcp_session import MCPQuerySession

QUERY = {
    "version": 2,
    "select": [{"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
}


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    yield adapter
    adapter.close()


def _no_hints(response: dict[str, Any]) -> None:
    assert "same_as" not in response and "already_ran" not in response


def test_repeat_executes_again_and_points_to_first_response(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    query = adapter.runtime.query

    def run(payload: Any) -> Any:
        calls.append(payload)
        return query(payload)

    monkeypatch.setattr(adapter.runtime, "query", run)
    session = MCPQuerySession()
    for index in range(3):
        response = adapter.call_tool(
            "execute", {"query": QUERY, "request_id": f"run-{index}"}, session=session
        )
        assert response["ok"] is True
        if index == 0:
            _no_hints(response)
        else:
            assert response["same_as"] == "run-0"
            assert "already_ran" not in response
    assert len(calls) == 3


@pytest.mark.parametrize("mode", ["validate", "sql"])
def test_dry_run_points_to_successful_run(adapter: SemanticLayerMCPAdapter, mode: str) -> None:
    session = MCPQuerySession()
    ran = adapter.call_tool("execute", {"query": QUERY, "row_format": "columns"}, session=session)
    dry_run = adapter.call_tool("execute", {"query": QUERY, "mode": mode}, session=session)
    assert dry_run["ok"] is True
    assert "same_as" not in dry_run
    assert dry_run["already_ran"] == {
        "request_id": ran["request_id"],
        "row_count": ran["row_count"],
    }
    repeated = adapter.call_tool("execute", {"query": QUERY, "mode": mode}, session=session)
    assert repeated["same_as"] == dry_run["request_id"]
    assert repeated["already_ran"] == dry_run["already_ran"]


@pytest.mark.parametrize(
    "options",
    [{"max_rows": 1}, {"verbosity": "full"}, {"query": {**QUERY, "verbosity": "compact"}}],
)
def test_response_options_and_object_key_order_do_not_distinguish_requests(
    adapter: SemanticLayerMCPAdapter, options: dict[str, Any]
) -> None:
    session = MCPQuerySession()
    first = adapter.call_tool("execute", {"query": QUERY}, session=session)
    reordered = {key: QUERY[key] for key in reversed(QUERY)}
    repeated = adapter.call_tool("execute", {"query": reordered, **options}, session=session)
    assert repeated["same_as"] == first["request_id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"query": {**QUERY, "group_by": ["dimension.jaffle_store_name"]}},
        {"query": {**QUERY, "limits": {"max_rows": 2}}},
        {"policy_context": {"audience": "another-audience"}},
        {"query": {**QUERY, "select": [{"expression": {"measure": "missing"}}]}},
    ],
)
def test_changed_arguments_get_no_hint(
    adapter: SemanticLayerMCPAdapter, changes: dict[str, Any]
) -> None:
    for mode in ("run", "validate", "sql"):
        session = MCPQuerySession()
        adapter.call_tool("execute", {"query": QUERY}, session=session)
        result = adapter.call_tool(
            "execute", {"query": QUERY, "mode": mode, **changes}, session=session
        )
        _no_hints(result)


@pytest.mark.parametrize("tool,arguments", [(t, a) for _, t, a in V2_PROBES])
def test_first_call_bytes_match_without_a_session(
    adapter: SemanticLayerMCPAdapter,
    tool: str,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Preview's unordered warehouse sample can differ even within one runtime.
    # Freeze that input so this comparison measures only the MCP boundary.
    if tool == "segment" and arguments.get("action") == "preview":
        preview = adapter.runtime.segment_preview(arguments["segment_id"])
        monkeypatch.setattr(adapter.runtime, "segment_preview", lambda *_a, **_k: deepcopy(preview))
    arguments = {**arguments, "request_id": "first-call"}
    without = adapter.call_tool(tool, arguments)
    with_session = adapter.call_tool(tool, arguments, session=MCPQuerySession())
    assert with_session["ok"] is True
    _no_hints(with_session)
    assert normalize_volatile(json_text(with_session)) == normalize_volatile(json_text(without))


def test_calls_without_sessions_and_other_sessions_are_isolated(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    args = {"query": QUERY}
    session = MCPQuerySession()
    adapter.call_tool("execute", args, session=session)
    for _ in range(2):
        _no_hints(adapter.call_tool("execute", args))
    _no_hints(adapter.call_tool("execute", args, session=MCPQuerySession()))
    other = SemanticLayerMCPAdapter(adapter.runtime)
    _no_hints(other.call_tool("execute", args, session=session))


def test_failed_runs_are_not_recorded_as_successful_answers(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    session = MCPQuerySession()
    adapter.replace_tool_handler("execute", lambda _: {"ok": False, "row_count": 7})
    failed = adapter.call_tool("execute", {"query": QUERY}, session=session)
    adapter.replace_tool_handler("execute", lambda _: {"ok": True, "row_count": 0})
    validated = adapter.call_tool("execute", {"query": QUERY, "mode": "validate"}, session=session)
    _no_hints(validated)
    succeeded = adapter.call_tool("execute", {"query": QUERY}, session=session)
    assert succeeded["same_as"] == failed["request_id"]
    compiled = adapter.call_tool("execute", {"query": QUERY, "mode": "sql"}, session=session)
    assert compiled["already_ran"] == {"request_id": succeeded["request_id"], "row_count": 0}


def test_lru_eviction_forgets_requests_and_successful_runs(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    session = MCPQuerySession()
    first = adapter.call_tool("execute", {"query": QUERY}, session=session)
    adapter.replace_tool_handler("discover", lambda _: {"ok": True})
    for index in range(63):
        adapter.call_tool("discover", {"terms": str(index)}, session=session)
    # A repeat refreshes recency without replacing its original reference.
    assert (
        adapter.call_tool("execute", {"query": QUERY}, session=session)["same_as"]
        == first["request_id"]
    )
    adapter.call_tool("discover", {"terms": "63"}, session=session)
    assert (
        adapter.call_tool("execute", {"query": QUERY}, session=session)["same_as"]
        == first["request_id"]
    )
    for index in range(64, 128):
        adapter.call_tool("discover", {"terms": str(index)}, session=session)
    _no_hints(adapter.call_tool("execute", {"query": QUERY, "mode": "validate"}, session=session))
    _no_hints(adapter.call_tool("execute", {"query": QUERY}, session=session))


def test_stdio_owns_one_history_per_connection(adapter: SemanticLayerMCPAdapter) -> None:
    def exchange() -> list[dict[str, Any]]:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": "execute", "arguments": {"query": QUERY, "mode": mode}},
            }
            for index, mode in enumerate(("run", "run", "validate"))
        ]
        output = io.StringIO()
        serve_stdio(
            adapter,
            input_stream=io.StringIO("\n".join(map(json.dumps, messages))),
            output_stream=output,
        )
        results = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
        for result in results:
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        return [result["structuredContent"] for result in results]

    for _ in range(2):
        first, repeat, validate = exchange()
        _no_hints(first)
        assert repeat["same_as"] == first["request_id"]
        assert validate["already_ran"]["request_id"] == first["request_id"]


@pytest.mark.parametrize(
    "first,changed",
    [
        ({"values": [1, 2]}, {"values": [2, 1]}),
        ({"where": [{"value": {"verbosity": "a"}}]}, {"where": [{"value": {"verbosity": "b"}}]}),
        ({"sql_profile": "default"}, {"sql_profile": "another"}),
    ],
)
def test_meaningful_nested_data_and_array_order_are_not_ignored(
    first: dict[str, Any], changed: dict[str, Any]
) -> None:
    session = MCPQuerySession()
    owner = object()
    session.annotate(owner, "execute", first, {"request_id": "first"})
    response = {"request_id": "changed"}
    session.annotate(owner, "execute", changed, response)
    _no_hints(response)


def test_unfingerprintable_arguments_cannot_match() -> None:
    session = MCPQuerySession()
    owner = object()
    arguments = {"value": object()}
    for _ in range(2):
        response = {"request_id": "request"}
        session.annotate(owner, "discover", arguments, response)
        _no_hints(response)


def test_empty_arguments_and_errors_also_have_repeat_hints(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    session = MCPQuerySession()
    first = adapter.call_tool("discover", session=session)
    assert adapter.call_tool("discover", session=session)["same_as"] == first["request_id"]
    failed = adapter.call_tool("inspect", {"object_id": "missing"}, session=session)
    repeat = adapter.call_tool("inspect", {"object_id": "missing"}, session=session)
    assert repeat["ok"] is False and repeat["same_as"] == failed["request_id"]
