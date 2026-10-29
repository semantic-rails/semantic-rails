"""MCP tool descriptions are agent guidance: when to call, a gotcha, a bounded length.

The workflow itself is stated once, in the server instructions (see test_mcp_instructions.py).
"""

from __future__ import annotations

import copy

from semantic_rails.mcp import list_tool_definitions

LOOP_KEYWORDS = ("after", "before", "step", "first")
GOTCHA_KEYWORDS = ("gotcha", "must be", "do not", "don't", "skip", "only after", "cost", "needs no")


def test_every_tool_description_says_when_and_how_to_call_it():
    tools = {tool["name"]: tool["description"] for tool in list_tool_definitions()}
    for name, description in tools.items():
        lowered = description.lower()
        assert any(kw in lowered for kw in LOOP_KEYWORDS), (name, description)
        assert any(kw in lowered for kw in GOTCHA_KEYWORDS), (name, description)
        # Non-trivial, never only an HTTP-equivalence stub, and still scannable.
        assert len(description) >= 200, (name, description)
        assert len(description.split("Equivalent to POST")[0].strip()) >= 150, (name, description)
        assert len(description) <= (850 if name == "execute" else 700), name
    # plan teaches the agent to branch on its empty-result blocks.
    assert "out_of_scope" in tools["plan"] or "low_relevance" in tools["plan"]
    # The positional item shapes are taught once, on execute.
    for marker in ("select", "group_by", "where/having [{field, op, value}]", "order_by"):
        assert marker in tools["execute"], marker


def test_execute_schema_is_the_call_and_empty_select_lists_rows(runtime_factory):
    tools = list_tool_definitions()
    execute = next(t for t in tools if t["name"] == "execute")
    execute_desc = execute["description"]
    # The query schema's kind table lists every inline kind; the description stays short.
    table = execute["inputSchema"]["$defs"]["Expression"]["description"].split("): ", 1)[1]
    kinds = [name for row in table.split(";") for name in row.split(":", 1)[0].split("/")]
    for kind in (
        "prior_period",
        "rolling",
        "cumulative",
        "ratio",
        "conversion",
        "aggregate_if",
        "between",
        "arithmetic",
    ):
        assert kind in kinds
    assert "group_by dimension ids (alone, it lists rows)" in execute_desc
    assert "'validate' only checks (skip it: run a call directly)" in execute_desc
    # ok is the call's validity, never a judgment that it answers the question.
    assert "'ok': valid and ran, not that it answers the question." in execute_desc
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        result = adapter.call_tool(
            "execute",
            {"mode": "run", "query": {"select": [], "group_by": ["dimension.jaffle_store_name"]}},
        )
        assert result["ok"] and result["rows"], result
        assert all(set(row) == {"dimension.jaffle_store_name"} for row in result["rows"])
    finally:
        adapter.close()


def test_no_orphan_explain_tool_references():
    """The `explain` tool was removed; the field on compile's response
    still exists. Tool descriptions and prompts must not reference
    `'explain'` (quoted) as a callable tool — only as a payload field
    (e.g. `compile's response includes an explain payload`)."""
    from semantic_rails.mcp import (
        MCP_PROMPT_DEFINITIONS,
        MCP_TOOL_DEFINITIONS,
    )

    for tool in MCP_TOOL_DEFINITIONS:
        desc = str(tool["description"])
        if tool["name"] == "segment":  # 'explain' is one of its actions
            continue
        assert "'explain'" not in desc, (
            f"tool {tool['name']!r} description references removed tool 'explain': {desc!r}"
        )
    for prompt in MCP_PROMPT_DEFINITIONS:
        desc = str(prompt["description"])
        # Bare ", and explain" in a comma list refers to the tool, not
        # the payload field.
        assert ", and explain" not in desc, (
            f"prompt {prompt['name']!r} lists removed tool 'explain': {desc!r}"
        )


def test_last_query_schema_describes_object_shape():
    """``time.last`` is an object ``{unit, value}``, never a string like '90 days'."""
    from semantic_rails.mcp import QUERY_SCHEMA

    time_block = QUERY_SCHEMA["properties"]["time"]
    assert "range" not in time_block["properties"]
    last = time_block["properties"]["last"]
    assert last["type"] == "object", f"time.last must be declared as object; got {last!r}"
    assert set(last["required"]) == {"unit", "value"}
    assert last["additionalProperties"] is False


def test_expression_shape_examples_validate_as_query_ir(runtime_factory):
    """Each shipped expression example, wrapped as a select item with placeholder ids
    replaced, validates without a shape error: it is the rescue path for advanced kinds."""
    from semantic_rails.mcp import SemanticLayerMCPAdapter
    from semantic_rails.query_call import EXPRESSION_SHAPES

    # Some shapes need scaffolding beyond a single select expression:
    # - conversion + aggregate_if reference column refs that must bind
    #   to a real entity column; jaffle_shop's order entity has a
    #   `status` column for aggregate_if and a `customer_id` for joins.
    # - distribution requires `over.entity` to be a real entity id.
    # The placeholder map below mirrors _replace_placeholder_ids but
    # adds `<col>` -> 'status' for the aggregate_if shape.
    placeholders = {
        "measure.<id>": "measure.jaffle.revenue_usd",
        "measure.<num>": "measure.jaffle.revenue_usd",
        "measure.<den>": "measure.jaffle.order_count",
        "measure.<start>": "measure.jaffle.session_count",
        "measure.<end>": "measure.jaffle.order_count",
        "metric.<id>": "metric.sales.aov_usd",
        "entity.<id>": "entity.jaffle_order",
        "<col>": "status",
    }

    def replace(node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, str) and value in placeholders:
                    node[key] = placeholders[value]
                else:
                    replace(value)
        elif isinstance(node, list):
            for item in node:
                replace(item)

    # Some kinds require an outer time block (e.g. prior_period offsets
    # imply a temporal anchor; cumulative inherits grain from outer
    # time.grain). Provide a minimal jaffle-compatible time block when
    # the kind asks for one — the parse-only test doesn't cover this.
    needs_time = {"prior_period", "cumulative", "rolling"}

    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        failures = []
        for shape in EXPRESSION_SHAPES:
            # Deep copy: the placeholder swap must not edit the shared shapes.
            example = copy.deepcopy(shape["example"])
            replace(example)
            query_ir: dict = {
                "select": [{"expression": example, "as": "_test"}],
            }
            if shape["name"] in needs_time:
                query_ir["time"] = {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                }
            response = adapter.call_tool("execute", {"mode": "validate", "query": query_ir})
            errors = response.get("errors") or []
            # Shape failures = any error pointing into select.expression
            # OR a structural code like INVALID_EXPRESSION_SHAPE.
            # Shape-related error codes that would indicate the example
            # itself is malformed (vs. a missing package object or
            # boundary issue we don't care about for this test).
            shape_codes = {
                "INVALID_EXPRESSION_AST",
                "UNKNOWN_EXPRESSION_KIND",
                "INVALID_EXPRESSION",
                "INVALID_EXPRESSION_SHAPE",
                "USE_OBJECT_SHAPE",
            }
            shape_errors = [
                e
                for e in errors
                if (
                    e.get("code") in shape_codes
                    or "expression" in str(e.get("path", "")).lower()
                    or "expression" in str(e.get("details", {}).get("path", "")).lower()
                    or "shape" in str(e.get("code", "")).lower()
                )
            ]
            if shape_errors:
                failures.append((shape["name"], shape_errors))
        assert not failures, (
            "capabilities.expression_shapes[].example must produce no "
            "shape-related validate errors when wrapped as Query IR. "
            f"Failures: {failures}"
        )
    finally:
        adapter.close()
