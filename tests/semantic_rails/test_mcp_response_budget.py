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
    list_tool_definitions,
)
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.mcp_session import MCPQuerySession
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

QUERY = {
    "select": [
        {"as": f"revenue_{i}", "expression": f"measure.sales.revenue_{i}"} for i in range(18)
    ],
}
VERBOSITIES = ("minimal", "compact", "full")


@pytest.fixture()
def jaffle_adapter(jaffle_snapshot) -> Iterator[SemanticLayerMCPAdapter]:
    adapter = SemanticLayerMCPAdapter(
        Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop")
    )
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
        "select": [{"as": "revenue", "expression": "measure.jaffle.revenue_usd"}],
    }
    monkeypatch.setattr(
        adapter.runtime,
        "query",
        lambda *_args, **_kwargs: {
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
        "select": [{"as": "revenue", "expression": "measure.jaffle.revenue_usd"}],
        "where": [
            {
                "field": "dimension.jaffle_order_customer_order_number",
                "op": "is",
                "value": [1, *[10**15 + i for i in range(3000)]],
            }
        ],
    }
    compiled = adapter.runtime.compile(query, verbosity=verbosity)
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
def medium_adapter(jaffle_snapshot) -> Iterator[SemanticLayerMCPAdapter]:
    base = Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop")
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


# The largest response of each tool and mode, and execute's compact form too.
CASES = [
    ("discover", {"terms": "revenue by store", "verbosity": "full"}),
    ("discover", {"terms": "", "verbosity": "full"}),
    ("inspect", {"object_id": "dimension.jaffle_store_name", "verbosity": "full"}),
    ("valid-values", {"dimension_id": "dimension.jaffle_store_name"}),
    *(("plan", {"intent": "revenue by store", "detail": detail}) for detail in ("full", "debug")),
    *(
        ("execute", {"query": QUERY, "mode": mode, "verbosity": verbosity})
        for mode in ("validate", "sql", "run")
        for verbosity in ("compact", "full")
    ),
    *(
        (
            "segment",
            {
                "segment_id": "segment.jaffle.high_value_customers",
                "action": action,
                "verbosity": "full",
                "limit": 1,
            },
        )
        for action in ("validate", "explain", "preview")
    ),
]


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
@pytest.mark.parametrize("verbosity", ["compact", "full"])
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
    monkeypatch.setattr(medium_adapter.runtime, method, lambda *_args, **_kwargs: payload.copy())
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
        lambda *_args, **_kwargs: {"ok": True, "rows": [{"value": 1}], "row_count": 1},
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


# Each order with its store and customer: ten columns keyed by full ids. At the default 200 rows
# its records run over 100,000 characters and its columns about 28,000.
ORDER_LISTING = {
    "group_by": [
        f"dimension.jaffle_{name}"
        for name in (
            "order_id",
            "order_ordered_at",
            "order_customer_order_number",
            "order_is_new_customer_order",
            "order_is_large_order",
            "order_has_food_item",
            "order_has_drink_item",
            "store_name",
            "customer_name",
            "customer_type",
        )
    ],
    "order_by": [{"field": "dimension.jaffle_order_id", "direction": "asc"}],
}


def _per_call(response: dict[str, Any]) -> dict[str, Any]:
    """``response`` without the fields every call sets afresh."""
    return {key: value for key, value in response.items() if key not in ("request_id", "timing_ms")}


def test_rows_too_wide_as_records_come_back_as_columns_with_a_note(
    jaffle_adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc = handle_jsonrpc_message(
        jaffle_adapter,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "execute", "arguments": {"query": ORDER_LISTING}},
        },
    )
    assert rpc is not None
    response = rpc["result"]["structuredContent"]
    assert json.loads(rpc["result"]["content"][0]["text"]) == response
    assert len(rpc["result"]["content"][0]["text"]) <= MCP_DEFAULT_MAX_RESULT_CHARS
    assert response["ok"], response
    assert response["row_format"] == "columns"
    assert response["columns"] == ORDER_LISTING["group_by"]
    assert response["row_count"] == len(response["rows"]) == 200
    *warnings, note = response["warnings"]
    assert [warning["code"] for warning in warnings] == ["EXECUTE_ROWS_TRUNCATED"]
    assert note["details"].pop("records_chars") > 100_000
    assert note == {
        "code": "ROW_FORMAT_COLUMNS",
        "severity": "info",
        "message": "Rows are returned as columns: as records they exceed the 32,000-character "
        "response budget. Each row is an array in the order of 'columns'.",
        "details": {"max_result_chars": MCP_DEFAULT_MAX_RESULT_CHARS},
    }
    # Exactly what the call asking for columns returns, beside the note.
    asked = jaffle_adapter.call_tool("execute", {"query": ORDER_LISTING, "row_format": "columns"})
    assert _per_call({**response, "warnings": warnings}) == _per_call(asked)
    # The same rows, in the same order, as the records an unlimited budget returns.
    monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", "10000000")
    records = jaffle_adapter.call_tool("execute", {"query": ORDER_LISTING})
    assert "row_format" not in records
    columns = response["columns"]
    assert [dict(zip(columns, row, strict=True)) for row in response["rows"]] == records["rows"]


def test_rows_that_fit_as_records_are_returned_as_records_unchanged(
    jaffle_adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = {"query": ORDER_LISTING, "max_rows": 50}
    response = jaffle_adapter.call_tool("execute", arguments)
    assert response["ok"], response
    assert "row_format" not in response and "columns" not in response
    assert all(isinstance(row, dict) for row in response["rows"])
    assert [warning["code"] for warning in response["warnings"]] == ["EXECUTE_ROWS_TRUNCATED"]
    monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", "10000000")
    unbudgeted = jaffle_adapter.call_tool("execute", arguments)
    assert json_text(_per_call(response)) == json_text(_per_call(unbudgeted))


@pytest.mark.parametrize(
    ("options", "rows"),
    [
        ({"max_rows": 300}, 300),
        ({"max_rows": 300, "row_format": "columns"}, 300),
        # Columns would fit, but the caller asked for records by name.
        ({"row_format": "records"}, 200),
    ],
    ids=["too-wide-as-columns", "asked-for-columns", "asked-for-records"],
)
def test_rows_that_cant_fit_as_asked_are_refused_as_before(
    jaffle_adapter: SemanticLayerMCPAdapter, options: dict[str, Any], rows: int
) -> None:
    response = jaffle_adapter.call_tool("execute", {"query": ORDER_LISTING, **options})
    assert not response["ok"] and "rows" not in response
    assert response["error"] == {"code": "RESULT_TOO_LARGE"}
    (issue,) = response["errors"]
    assert issue["details"].pop("result_chars") > MCP_DEFAULT_MAX_RESULT_CHARS
    records = options.get("row_format") != "columns"
    assert issue == {
        "code": "RESULT_TOO_LARGE",
        "message": f"The result has more than {rows} rows and exceeds the 32,000-character "
        "response budget without optional details. No rows returned. Retry "
        + ('with row_format "columns", ' if records else "")
        + "selecting fewer columns ('dimension.jaffle_order_id' is the largest); or filter, or "
        "group by fewer dimensions, or lower max_rows.",
        "details": {
            "max_result_chars": MCP_DEFAULT_MAX_RESULT_CHARS,
            "largest_field": "dimension.jaffle_order_id",
            **({"retry": {"row_format": "columns"}} if records else {}),
            "row_count": rows,
            "total_row_count": None,
        },
    }


def test_rows_too_wide_as_records_take_one_warehouse_query(
    jaffle_adapter: SemanticLayerMCPAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    warehouse = jaffle_adapter.runtime._get_adapter()
    query = warehouse.query
    statements: list[str] = []

    def counted(sql: str, **options: Any) -> Any:
        statements.append(sql)
        return query(sql, **options)

    monkeypatch.setattr(warehouse, "query", counted)
    # An agent following the refusal's retry hint until the rows arrive.
    arguments: dict[str, Any] = {"query": ORDER_LISTING}
    response = jaffle_adapter.call_tool("execute", arguments)
    while not response["ok"] and len(statements) < 3:
        arguments = {**arguments, **response["errors"][0]["details"]["retry"]}
        response = jaffle_adapter.call_tool("execute", arguments)
    assert response["ok"] and response["row_count"] == 200
    assert len(statements) == 1


@pytest.mark.parametrize(
    ("expression", "kind", "path"),
    [
        (
            {"kind": "group", "dimensions": ["dimension.store_name"]},
            "group",
            "select[0].expression.kind",
        ),
        (
            {
                "kind": "arithmetic",
                "op": "+",
                "left": {"kind": "ref"},
                "right": {"kind": "literal", "value": 1},
            },
            "ref",
            "select[0].expression.left.kind",
        ),
        ({"kind": 42}, 42, "select[0].expression.kind"),
    ],
)
def test_unsupported_expression_reports_received_kind_and_request_path(
    medium_adapter: SemanticLayerMCPAdapter, expression: dict[str, Any], kind: Any, path: str
) -> None:
    result = medium_adapter.call_tool(
        "execute", {"query": {"select": [{"expression": expression}]}}
    )
    assert not result["ok"]
    issue = result["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert issue["details"]["path"] == path
    assert "measure" in issue["details"]["supported_kinds"]
    assert f"'{kind}'" in issue["message"]
    assert len(issue["recovery_hints"]) == 1
    assert "docs/" not in json_text(result) and "schemas/" not in json_text(result)


# Every agent session pays for tools/list. Each tool's definition as it is sent (compact JSON);
# execute embeds the call's schema with each expression kind's keys as one line of a table.
# A budget may only shrink.
TOOL_MAX_BYTES = {
    "discover": 1_732,
    "inspect": 1_123,
    "valid-values": 1_388,
    "plan": 1_887,
    "execute": 6_463,  # the call's totals field
    "segment": 1_300,
}


def test_each_tool_definition_fits_its_byte_budget() -> None:
    sizes = {
        tool["name"]: len(json.dumps(tool, separators=(",", ":")))
        for tool in list_tool_definitions()
    }
    assert sizes.keys() == TOOL_MAX_BYTES.keys()
    assert {name: size for name, size in sizes.items() if size > TOOL_MAX_BYTES[name]} == {}


def test_tools_list_fits_its_byte_budget() -> None:
    assert len(json_text({"tools": list_tool_definitions()})) <= 13_910  # discover's kinds


# discover is most of an agent's tokens. Its default responses on the jaffle package, without
# the per-call request_id and timing; a budget may only shrink.
DISCOVER_MAX_BYTES = {"revenue by store": 4_261, "": 9_393}


@pytest.mark.parametrize("terms", sorted(DISCOVER_MAX_BYTES))
def test_default_discover_fits_its_byte_budget(
    jaffle_adapter: SemanticLayerMCPAdapter, terms: str
) -> None:
    response = jaffle_adapter.call_tool("discover", {"terms": terms})
    assert response["ok"], response
    measured = {k: v for k, v in response.items() if k not in {"request_id", "timing_ms"}}
    assert len(json_text(measured)) <= DISCOVER_MAX_BYTES[terms]
