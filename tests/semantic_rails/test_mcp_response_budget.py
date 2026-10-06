"""All tool modes budget their final payload and preserve answers over plan detail."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.mcp import (
    MCP_DEFAULT_MAX_RESULT_CHARS,
    MCP_MIN_RESULT_CHARS,
    SemanticLayerMCPAdapter,
    json_text,
)
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.mcp_session import MCPQuerySession
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime

QUERY = {
    "version": 1,
    "select": [
        {"as": f"revenue_{i}", "expression": {"measure": f"measure.sales.revenue_{i}"}}
        for i in range(18)
    ],
}
VERBOSITIES = ("minimal", "compact", "full")


@pytest.fixture()
def jaffle_adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.mark.parametrize("oversized", ["rows", "annotations"])
def test_refused_run_never_advises_using_an_undelivered_answer(
    jaffle_adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch, oversized: str
) -> None:
    adapter = jaffle_adapter
    query = {
        "version": 1,
        "select": [{"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
    }
    monkeypatch.setattr(
        adapter.runtime,
        "query",
        lambda _: {
            "ok": True,
            "row_count": 1,
            "rows": [{"revenue": "x" * 50_000 if oversized == "rows" else 1}],
        },
    )
    session = MCPQuerySession()
    annotate = session.annotate

    def large_annotation(*args: Any, **kwargs: Any) -> None:
        annotate(*args, **kwargs)
        if args[1] == "execute" and args[2].get("mode", "run") == "run":
            args[3]["next"] = "x" * 50_000

    if oversized == "annotations":
        monkeypatch.setattr(session, "annotate", large_annotation)
    refused = adapter.call_tool("execute", {"query": query}, session=session)
    assert not refused["ok"] and refused["errors"][0]["code"] == "RESULT_TOO_LARGE"
    assert "rows" not in refused
    for mode in ("validate", "sql"):
        response = adapter.call_tool("execute", {"query": query, "mode": mode}, session=session)
        assert response["ok"], response
        assert "already_ran" not in response
        assert "answer from" not in str(response.get("next", "")).lower()


@pytest.mark.parametrize("verbosity", ["compact", "full"])
def test_large_rendered_sql_is_optional_only_for_a_run(
    jaffle_adapter: SemanticLayerMCPAdapter, verbosity: str
) -> None:
    adapter = jaffle_adapter
    query = {
        "version": 1,
        "select": [{"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
        "where": [
            {
                "field": "dimension.jaffle_order_customer_order_number",
                "op": "in",
                "value": [1, *[10**15 + i for i in range(3000)]],
            }
        ],
    }
    compiled = adapter.runtime.compile({**query, "verbosity": verbosity})
    assert len(compiled["rendered_sql"]) > MCP_DEFAULT_MAX_RESULT_CHARS
    expected = adapter.runtime._get_adapter().query(
        "SELECT SUM(order_total_cents / 100.0) AS revenue FROM jaffle_order "
        "WHERE customer_order_number = 1"
    )
    response = adapter.call_tool("execute", {"query": query, "verbosity": verbosity})
    assert response["ok"], response
    assert response["row_count"] == 1 and response["rows"] == expected
    assert "rendered_sql" not in response
    assert "rendered_sql" in response["omitted_fields"]
    assert len(json_text(response)) <= MCP_DEFAULT_MAX_RESULT_CHARS
    sql = adapter.call_tool("execute", {"query": query, "mode": "sql", "verbosity": verbosity})
    assert not sql["ok"] and sql["errors"][0]["code"] == "RESULT_TOO_LARGE"
    assert "rendered_sql" not in sql


@pytest.fixture()
def medium_adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    base = runtime_factory("jaffle_shop")
    config = base.config
    measure = next(m for m in config.measures if m.id == "measure.jaffle.revenue_usd")
    metric = next(m for m in config.metric_recipes if m.id == "metric.sales.aov_usd")
    dimension = next(d for d in config.dimensions if d.id == "dimension.jaffle_store_name")
    config = replace(
        config,
        measures=[
            *config.measures,
            *[
                replace(
                    measure,
                    id=f"measure.sales.revenue_{i}",
                    name=f"revenue_{i}",
                    label=f"Revenue {i}",
                    description="Total sales revenue across orders.",
                )
                for i in range(48)
            ],
        ],
        metric_recipes=[
            *config.metric_recipes,
            *[
                replace(
                    metric,
                    id=f"metric.sales.value_{i}",
                    name=f"value_{i}",
                    label=f"Average order value {i}",
                )
                for i in range(24)
            ],
        ],
        dimensions=[
            *config.dimensions,
            *[
                replace(
                    dimension,
                    id=f"dimension.sales_store_{i}",
                    name=f"store_{i}",
                    label=f"Store {i}",
                )
                for i in range(24)
            ],
        ],
    )
    adapter = SemanticLayerMCPAdapter(Runtime.from_config(config, source_path=base.source_path))
    adapter.runtime._get_adapter()
    try:
        yield adapter
    finally:
        adapter.close()
        base.close()


CASES = (
    [
        ("discover", {"terms": "revenue by store", "verbosity": verbosity})
        for verbosity in VERBOSITIES
    ]
    + [("discover", {"terms": "", "verbosity": verbosity}) for verbosity in VERBOSITIES]
    + [
        ("inspect", {"object_id": "dimension.jaffle_store_name", "verbosity": verbosity})
        for verbosity in VERBOSITIES
    ]
    + [
        ("valid-values", {"dimension_id": "dimension.jaffle_store_name"}),
    ]
    + [
        ("plan", {"intent": "revenue by store", "detail": detail})
        for detail in ("query", "best", "full", "debug")
    ]
    + [
        ("execute", {"query": QUERY, "mode": mode, "verbosity": verbosity})
        for mode in ("validate", "sql", "run")
        for verbosity in VERBOSITIES
    ]
    + [
        (
            "segment",
            {
                "segment_id": "segment.jaffle.high_value_customers",
                "action": action,
                "verbosity": verbosity,
                "limit": 1,
            },
        )
        for action in ("validate", "explain", "preview")
        for verbosity in VERBOSITIES
    ]
)


@pytest.mark.parametrize(("tool", "arguments"), CASES)
def test_medium_package_tool_modes_fit_both_content_channels(
    medium_adapter: SemanticLayerMCPAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    rpc = handle_jsonrpc_message(
        medium_adapter,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert rpc is not None
    result = rpc["result"]
    payload = result["structuredContent"]
    assert payload["ok"], payload
    assert json.loads(result["content"][0]["text"]) == payload
    assert len(result["content"][0]["text"]) <= MCP_DEFAULT_MAX_RESULT_CHARS
    assert len(json_text(payload)) <= MCP_DEFAULT_MAX_RESULT_CHARS
    assert "logical_plan" not in payload
    if tool == "execute":
        if arguments["verbosity"] == "compact":
            assert not {"explain", "sql_plan", "physical_plan", "performance_plan"} & payload.keys()
        if arguments["mode"] == "sql":
            assert payload["rendered_sql"]
        if arguments["mode"] == "run":
            assert payload["row_count"] == len(payload["rows"]) == 1


@pytest.mark.parametrize("verbosity", VERBOSITIES)
def test_one_row_matches_reference_sql_despite_large_plan(
    medium_adapter: SemanticLayerMCPAdapter, verbosity: str
) -> None:
    # The eighteen independent selects make a large plan but a single answer row.
    response = medium_adapter.call_tool("execute", {"query": QUERY, "verbosity": verbosity})
    assert response["ok"], response
    assert response["row_count"] == 1
    expected = medium_adapter.runtime._get_adapter().query(
        "SELECT SUM(order_total_cents / 100.0) AS revenue FROM jaffle_order"
    )[0]["revenue"]
    assert response["rows"] == [{f"revenue_{i}": expected for i in range(18)}]
    assert "logical_plan" not in response
    if verbosity != "minimal":
        assert response["omitted_fields"]


@pytest.mark.parametrize("mode", ["validate", "sql", "run"])
@pytest.mark.parametrize("verbosity", VERBOSITIES)
def test_plan_size_never_refuses_an_answer(
    medium_adapter: SemanticLayerMCPAdapter,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    verbosity: str,
) -> None:
    plans = {
        field: {"detail": "p" * 100_000}
        for field in (
            "logical_plan",
            "explain",
            "sql_plan",
            "physical_plan",
            "performance_plan",
            "fanout_analysis",
        )
    }
    payload = {"ok": True, "rows": [{"value": 12.5}], "row_count": 1, **plans}
    if mode == "sql":
        payload["rendered_sql"] = "SELECT 12.5 AS value"
    method = {"validate": "validate", "sql": "compile", "run": "query"}[mode]
    monkeypatch.setattr(medium_adapter.runtime, method, lambda _query: payload.copy())
    result = medium_adapter.call_tool(
        "execute", {"query": QUERY, "mode": mode, "verbosity": verbosity}
    )
    assert result["ok"] and result["rows"] == payload["rows"]
    assert len(json_text(result)) <= MCP_DEFAULT_MAX_RESULT_CHARS
    assert set(plans) <= set(result["omitted_fields"])
    assert not set(plans) & result.keys()


@pytest.mark.parametrize("verbosity", VERBOSITIES)
def test_budget_counts_transport_context_and_session_annotations(
    medium_adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch, verbosity: str
) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", "2000")
    monkeypatch.setattr(
        medium_adapter.runtime,
        "query",
        lambda _query: {"ok": True, "rows": [{"value": 1}], "row_count": 1},
    )
    context = RequestContext(actor="subject" * 500, request_id="budget-test")
    session = MCPQuerySession()
    arguments = {"query": QUERY, "verbosity": verbosity}
    for _ in range(2):
        result = medium_adapter.call_tool(
            "execute", arguments, request_context=context, session=session
        )
        assert result["ok"] and result["rows"] == [{"value": 1}]
        assert len(json_text(result)) <= 2000
        assert "request_context" in result["omitted_fields"]


@pytest.mark.parametrize(
    "tool", ["discover", "inspect", "valid-values", "plan", "execute", "segment"]
)
def test_replacement_handlers_cannot_bypass_the_budget(
    medium_adapter: SemanticLayerMCPAdapter, tool: str
) -> None:
    arguments = next(arguments for name, arguments in CASES if name == tool)
    medium_adapter.replace_tool_handler(
        tool, lambda _args: {"ok": True, "required_data": "x" * 50_000}
    )
    result = medium_adapter.call_tool(tool, arguments)
    assert not result["ok"]
    assert result["errors"][0]["code"] == "RESULT_TOO_LARGE"
    assert len(json_text(result)) <= MCP_DEFAULT_MAX_RESULT_CHARS


@pytest.mark.parametrize("limit", [1, 512, 2000])
def test_oversized_errors_and_tiny_operator_budgets_are_bounded(
    medium_adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", str(limit))
    result = medium_adapter.call_tool("execute", {"mode": "x" * 50_000, "query": QUERY})
    assert not result["ok"]
    assert result["errors"][0]["code"] == "RESULT_TOO_LARGE"
    assert len(json_text(result)) <= max(limit, MCP_MIN_RESULT_CHARS)


@pytest.mark.parametrize("mode", ["validate", "sql", "run"])
@pytest.mark.parametrize(
    ("expression", "kind", "path"),
    [
        (
            {"kind": "group", "dimensions": ["dimension.store_name"]},
            "group",
            "query.select[0].expression",
        ),
        (
            {
                "kind": "arithmetic",
                "op": "+",
                "left": {"kind": "ref"},
                "right": {"kind": "literal", "value": 1},
            },
            "ref",
            "query.select[0].expression.left",
        ),
        ({"kind": 42}, 42, "query.select[0].expression"),
    ],
)
def test_unsupported_expression_reports_received_kind_and_request_path(
    medium_adapter: SemanticLayerMCPAdapter,
    mode: str,
    expression: dict[str, Any],
    kind: Any,
    path: str,
) -> None:
    result = medium_adapter.call_tool(
        "execute",
        {"mode": mode, "query": {"version": 1, "select": [{"expression": expression}]}},
    )
    assert not result["ok"]
    issue = result["errors"][0]
    assert issue["code"] == "INVALID_EXPRESSION_AST"
    assert issue["details"]["expression_kind"] == kind
    assert issue["details"]["path"] == issue["details"]["expression_position"] == path
    assert repr(kind) in issue["message"] and path in issue["message"]
    assert "group_by" in issue["message"] and "measure" in issue["message"]
    assert "docs/" not in json_text(result) and "schemas/" not in json_text(result)
