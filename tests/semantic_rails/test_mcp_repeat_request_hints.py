"""Session hints refer to earlier calls without changing execution or first responses."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest

from scripts.mcp_context import V2_PROBES, normalize_volatile
from semantic_rails.mcp import MCP_DEFAULT_MAX_ROWS, SemanticLayerMCPAdapter, json_text
from semantic_rails.mcp_server import serve_stdio
from semantic_rails.mcp_session import MCPQuerySession
from semantic_rails.mcp_streamable_http import handle_streamable_http_request

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
    assert not isinstance(response.get("next"), str)


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
    query = {**QUERY, "group_by": ["dimension.jaffle_store_name"], "limits": {"max_rows": 2}}
    ran = adapter.call_tool("execute", {"query": query, "row_format": "columns"}, session=session)
    assert ran["row_count"] == 2
    args = {"query": query, "mode": mode, "request_id": "dry-run"}
    dry_run = adapter.call_tool("execute", args, session=session)
    assert dry_run["ok"] is True
    assert "same_as" not in dry_run
    assert dry_run["already_ran"] == {
        "request_id": ran["request_id"],
        "row_count": ran["row_count"],
    }
    assert dry_run["next"] == (
        "This query already ran in this session. Revalidating changes nothing; "
        "answer from that result, or change the query."
    )
    repeated = adapter.call_tool("execute", args, session=session)
    assert repeated["same_as"] == dry_run["request_id"]
    assert repeated["already_ran"] == dry_run["already_ran"]
    if mode == "validate":
        assert repeated["next"] == (
            "Stop validating this unchanged query. Answer from the prior result, "
            "or change the query."
        )
    else:
        assert repeated["next"] == dry_run["next"]
    third = adapter.call_tool("execute", args, session=session)
    assert third["next"] == repeated["next"]
    assert len(dry_run["next"]) < 160 and len(repeated["next"]) < 160
    # All validation/SQL output is identical to a normal call, apart from hints.
    without = adapter.call_tool("execute", args)
    for response in (dry_run, repeated, third):
        assert "rows" not in response
        unchanged = {
            k: v for k, v in response.items() if k not in {"same_as", "already_ran", "next"}
        }
        assert normalize_volatile(json_text(unchanged)) == normalize_volatile(json_text(without))


@pytest.mark.parametrize("mode", ["validate", "sql"])
def test_dry_runs_before_execution_have_no_guidance(
    adapter: SemanticLayerMCPAdapter, mode: str
) -> None:
    session = MCPQuerySession()
    first = adapter.call_tool("execute", {"query": QUERY, "mode": mode}, session=session)
    _no_hints(first)
    repeated = adapter.call_tool("execute", {"query": QUERY, "mode": mode}, session=session)
    assert repeated["same_as"] == first["request_id"]
    assert "already_ran" not in repeated and "next" not in repeated


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("discover", {"terms": "orders"}),
        ("execute", {"query": QUERY}),
        ("execute", {"query": QUERY, "mode": "sql"}),
        ("execute", {"query": QUERY, "mode": "validate", "sql_profile": "compact"}),
        ("execute", {"query": {**QUERY, "limits": {"max_rows": 2}}, "mode": "validate"}),
        ("execute", {"query": QUERY, "mode": "validate", "policy_context": {"audience": "other"}}),
        ("execute", {"query": {"select": "invalid"}, "mode": "validate"}),
    ],
)
def test_intervening_calls_reset_validate_guidance(
    adapter: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    session = MCPQuerySession()
    adapter.call_tool("execute", {"query": QUERY}, session=session)
    args = {"query": QUERY, "mode": "validate"}
    first = adapter.call_tool("execute", args, session=session)
    adapter.call_tool(tool, arguments, session=session)
    after = adapter.call_tool("execute", args, session=session)
    assert after["next"] == first["next"]


def test_response_options_do_not_reset_validate_guidance(adapter: SemanticLayerMCPAdapter) -> None:
    session = MCPQuerySession()
    adapter.call_tool("execute", {"query": QUERY}, session=session)
    adapter.call_tool("execute", {"query": QUERY, "mode": "validate"}, session=session)
    reordered = {key: QUERY[key] for key in reversed(QUERY)}
    repeated = adapter.call_tool(
        "execute",
        {"query": reordered, "mode": "validate", "verbosity": "full", "request_id": "repeat"},
        session=session,
    )
    assert repeated["next"].startswith("Stop validating")


@pytest.mark.parametrize("transport", ["in-process", "http"])
def test_stateless_dry_runs_never_get_session_guidance(
    adapter: SemanticLayerMCPAdapter, transport: str
) -> None:
    def call(mode: str) -> dict[str, Any]:
        arguments = {"query": QUERY, "mode": mode}
        if transport == "in-process":
            return adapter.call_tool("execute", arguments)
        response = handle_streamable_http_request(
            adapter,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            body=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "execute", "arguments": arguments},
                }
            ).encode(),
        )
        assert response.status == 200 and response.payload is not None
        return response.payload["result"]["structuredContent"]

    for mode in ("run", "validate", "validate", "sql"):
        result = call(mode)
        assert result["ok"] is True
        _no_hints(result)


@pytest.mark.parametrize(
    "options",
    [{"verbosity": "full"}, {"query": {**QUERY, "verbosity": "compact"}}],
)
def test_response_options_and_object_key_order_do_not_distinguish_requests(
    adapter: SemanticLayerMCPAdapter, options: dict[str, Any]
) -> None:
    session = MCPQuerySession()
    first = adapter.call_tool("execute", {"query": QUERY}, session=session)
    reordered = {key: QUERY[key] for key in reversed(QUERY)}
    repeated = adapter.call_tool("execute", {"query": reordered, **options}, session=session)
    assert repeated["same_as"] == first["request_id"]


@pytest.mark.parametrize("cap", [1, None])
@pytest.mark.parametrize("mode", ["validate", "sql"])
def test_capped_run_then_full_run_reports_the_latest_answer(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch, cap: int | None, mode: str
) -> None:
    row_count = 5 if cap is not None else MCP_DEFAULT_MAX_ROWS + 5
    rows = [{"revenue": index} for index in range(row_count)]
    monkeypatch.setattr(
        adapter.runtime, "query", lambda _: {"ok": True, "rows": rows, "row_count": len(rows)}
    )
    capped_args = {"query": QUERY, **({"max_rows": cap} if cap is not None else {})}
    full_args = {"query": QUERY, **({"max_rows": row_count} if cap is None else {})}
    session = MCPQuerySession()
    capped = adapter.call_tool("execute", capped_args, session=session)
    summary = {
        "request_id": capped["request_id"],
        "row_count": cap or MCP_DEFAULT_MAX_ROWS,
        "truncated": True,
        "max_rows": cap or MCP_DEFAULT_MAX_ROWS,
    }
    assert capped["row_count"] == summary["row_count"]
    assert capped["truncated"] is True
    dry_args = {"query": QUERY, "mode": mode}
    assert adapter.call_tool("execute", dry_args, session=session)["already_ran"] == summary
    assert adapter.call_tool("execute", capped_args, session=session)["same_as"] == summary

    full = adapter.call_tool("execute", full_args, session=session)
    assert full["row_count"] == row_count
    assert not full.get("truncated", False)
    _no_hints(full)
    assert adapter.call_tool("execute", dry_args, session=session)["already_ran"] == {
        "request_id": full["request_id"],
        "row_count": row_count,
    }
    assert adapter.call_tool("execute", full_args, session=session)["same_as"] == full["request_id"]

    refreshed = adapter.call_tool("execute", capped_args, session=session)
    assert refreshed["same_as"] == summary
    assert adapter.call_tool("execute", dry_args, session=session)["already_ran"] == {
        **summary,
        "request_id": refreshed["request_id"],
    }


def test_same_as_preserves_historical_truncation_when_current_rows_shrink(
    adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [{"revenue": index} for index in range(5)]
    monkeypatch.setattr(
        adapter.runtime, "query", lambda _: {"ok": True, "rows": rows, "row_count": len(rows)}
    )
    session = MCPQuerySession()
    args = {"query": QUERY, "max_rows": 1}
    first = adapter.call_tool("execute", args, session=session)
    rows[:] = rows[:1]
    current = adapter.call_tool("execute", args, session=session)
    assert not current.get("truncated", False)
    assert current["same_as"] == {
        "request_id": first["request_id"],
        "row_count": 1,
        "truncated": True,
        "max_rows": 1,
    }
    assert adapter.call_tool("execute", {"query": QUERY, "mode": "sql"}, session=session)[
        "already_ran"
    ] == {"request_id": current["request_id"], "row_count": 1}


@pytest.mark.parametrize("latest_options", [{}, {"row_format": "columns"}])
def test_successful_run_refreshes_history_and_a_failure_does_not_replace_it(
    adapter: SemanticLayerMCPAdapter,
    monkeypatch: pytest.MonkeyPatch,
    latest_options: dict[str, Any],
) -> None:
    rows = [{"revenue": index} for index in range(5)]
    monkeypatch.setattr(
        adapter.runtime, "query", lambda _: {"ok": True, "rows": rows, "row_count": len(rows)}
    )
    session = MCPQuerySession()
    adapter.call_tool("execute", {"query": QUERY}, session=session)
    rows[:] = rows[:3]
    latest = adapter.call_tool("execute", {"query": QUERY, **latest_options}, session=session)
    assert adapter.call_tool("execute", {"query": QUERY, "mode": "validate"}, session=session)[
        "already_ran"
    ] == {"request_id": latest["request_id"], "row_count": 3}
    monkeypatch.setattr(adapter.runtime, "query", lambda _: {"ok": False, "row_count": 99})
    assert adapter.call_tool("execute", {"query": QUERY}, session=session)["ok"] is False
    assert adapter.call_tool("execute", {"query": QUERY, "mode": "validate"}, session=session)[
        "already_ran"
    ] == {"request_id": latest["request_id"], "row_count": 3}


def test_truncated_run_without_a_known_cap_cannot_be_reused_as_an_answer(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    adapter.replace_tool_handler(
        "execute", lambda _: {"ok": True, "row_count": 1, "truncated": True}
    )
    session = MCPQuerySession()
    for mode in ("run", "run", "validate"):
        _no_hints(adapter.call_tool("execute", {"query": QUERY, "mode": mode}, session=session))


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
        adapter.call_tool("execute", {"query": QUERY, "mode": "run"}, session=session)
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
        assert validate["already_ran"]["request_id"] == repeat["request_id"]


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
