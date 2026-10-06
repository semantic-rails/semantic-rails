"""Spelling normalization preserves canonical results and rejects conflicting input."""

from __future__ import annotations

import json

import duckdb
import pytest

from semantic_rails.diagnostics import exception_issue
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_query import normalize_query_spellings
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.request_context import RequestContext

MEASURE = {"measure": "measure.jaffle.order_count"}
DIMENSION = "dimension.jaffle_order_customer_id"
QUERY = {"select": [{"expression": MEASURE, "as": "n"}]}


@pytest.fixture
def adapter(runtime_factory):
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    yield adapter
    adapter.close()


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("eq", "="),
        ("equals", "="),
        ("neq", "!="),
        ("gt", ">"),
        ("gte", ">="),
        ("lt", "<"),
        ("lte", "<="),
        ("is_not_null", "IS NOT NULL"),
    ],
)
def test_comparison_spellings_match_canonical_rows(adapter, alias, canonical):
    predicate = {"field": DIMENSION, "op": alias, "value": "customer_1"}
    canonical_query = {**QUERY, "where": [{**predicate, "op": canonical}]}
    expected = adapter.call_tool("execute", {"query": canonical_query})
    actual = adapter.call_tool("execute", {"query": {**QUERY, "where": [predicate]}})
    assert actual["ok"] and expected["ok"], actual
    assert actual["rows"] == expected["rows"]
    assert actual["normalized"] == [f"query.where[0].op: {alias} -> {canonical}"]


@pytest.mark.parametrize("op,sql_op", [("add", "+"), ("sub", "-"), ("mul", "*"), ("div", "/")])
@pytest.mark.parametrize("shape", ["binary", "operands", "terms"])
def test_arithmetic_spellings_match_reference_sql(adapter, op, sql_op, shape):
    operands = [MEASURE, {"kind": "literal", "value": 2}, {"kind": "literal", "value": 3}]
    expr = {"kind": "arithmetic", "op": op}
    if shape == "binary":
        expr.update(left=operands[0], right=operands[1])
        reference = f"SELECT COUNT(DISTINCT order_id) {sql_op} 2 FROM jaffle_order"
    else:
        expr[shape] = operands
        reference = f"SELECT (COUNT(DISTINCT order_id) {sql_op} 2) {sql_op} 3 FROM jaffle_order"
    actual = adapter.call_tool("execute", {"query": {"select": [{"expression": expr, "as": "n"}]}})
    assert actual["ok"], actual
    with duckdb.connect(adapter.runtime.db_path, read_only=True) as db:
        expected = db.execute(reference).fetchone()[0]
    assert actual["rows"][0]["n"] == pytest.approx(expected)
    if shape != "binary" or op != "add":
        assert actual["normalized"]


@pytest.mark.parametrize(
    "item",
    [
        {"dimension": DIMENSION},
        {"expression": {"dimension": DIMENSION}},
        *[{"kind": kind, "dimension": DIMENSION} for kind in ("dimension", "group", "ref")],
        *[
            {"expression": {"kind": kind, "dimension": DIMENSION}}
            for kind in ("dimension", "group", "ref")
        ],
    ],
)
@pytest.mark.parametrize("mode", ["run", "sql", "validate"])
@pytest.mark.parametrize("surface", ["mcp", "runtime"])
def test_selected_dimension_moves_to_group_by(adapter, item, mode, surface):
    canonical = adapter.call_tool(
        "execute",
        {
            "query": {
                "select": [],
                "group_by": [DIMENSION],
                "order_by": [{"field": DIMENSION}],
                "limit": 20,
            }
        },
    )
    query = {"select": [item], "order_by": [{"field": DIMENSION}], "limit": 20}
    if surface == "mcp":
        actual = adapter.call_tool("execute", {"mode": mode, "query": query})
    else:
        actual = {
            "run": adapter.runtime.query,
            "sql": adapter.runtime.compile,
            "validate": adapter.runtime.validate,
        }[mode](query)
    assert actual["ok"] and canonical["ok"], actual
    if mode == "run":
        assert actual["rows"] == canonical["rows"]
        with duckdb.connect(adapter.runtime.db_path, read_only=True) as db:
            expected = db.execute(
                "SELECT DISTINCT customer_id FROM jaffle_order ORDER BY customer_id LIMIT 20"
            ).fetchall()
        assert actual["rows"] == [{DIMENSION: row[0]} for row in expected]
    warning = next(w for w in actual["warnings"] if w["code"] == "QUERY_SHORTHAND_NORMALIZED")
    assert warning["details"]["canonical"] == {"group_by": [DIMENSION]}
    assert "normalized" not in actual


@pytest.mark.parametrize("surface", ["mcp", "runtime"])
@pytest.mark.parametrize("mode", ["run", "sql", "validate"])
@pytest.mark.parametrize(
    "item,groups,hint",
    [
        (
            {
                "dimension": "dimension.jaffle_order_store_id",
                "expression": {"dimension": DIMENSION},
            },
            [],
            "WRAP_SELECT_EXPRESSION",
        ),
        *[
            (
                {"expression": {**({"kind": kind} if kind else {}), "dimension": DIMENSION}},
                ["dimension.jaffle_order_store_id"],
                "MOVE_DIMENSION_TO_GROUP_BY",
            )
            for kind in (None, "dimension", "group", "ref")
        ],
    ],
)
def test_selected_dimension_ambiguity_refuses_on_both_surfaces(
    adapter, surface, mode, item, groups, hint
):
    query = {"select": [item], "group_by": groups}
    if surface == "mcp":
        out = adapter.call_tool("execute", {"mode": mode, "query": query})
    else:
        handler = {
            "run": adapter.runtime.query,
            "sql": adapter.runtime.compile,
            "validate": adapter.runtime.validate,
        }[mode]
        if mode != "validate":
            with pytest.raises(SemanticLayerError) as excinfo:
                handler(query)
            out = {"ok": False, "errors": [exception_issue(excinfo.value, stage="compile")]}
        else:
            out = handler(query)
    assert not out["ok"], out
    issue = out["errors"][0]
    assert issue["code"] == "INVALID_EXPRESSION_AST"
    assert hint in {h["code"] for h in issue["details"]["recovery_hints"]}


@pytest.mark.parametrize("transport", ["direct", "jsonrpc"])
@pytest.mark.parametrize(
    "kind,code", [("unknown", "INVALID_EXPRESSION_AST"), ("arithmetic", "INVALID_QUERY")]
)
def test_deep_expression_input_is_a_structured_refusal(adapter, transport, kind, code):
    nested = 0 if kind == "unknown" else MEASURE
    for _ in range(994):
        nested = (
            [nested]
            if kind == "unknown"
            else {
                "kind": "arithmetic",
                "op": "add",
                "left": nested,
                "right": {"kind": "literal", "value": 1},
            }
        )
    arguments = {
        "mode": "validate",
        "query": {
            "select": [
                {"expression": {"kind": kind, "data": nested} if kind == "unknown" else nested}
            ]
        },
    }
    if transport == "direct":
        out = adapter.call_tool("execute", arguments)
    else:
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "execute", "arguments": arguments},
        }
        response = handle_jsonrpc_message(adapter, message)
        assert response is not None and "result" in response, response
        assert response["result"]["isError"] is True
        out = response["result"]["structuredContent"]
    assert not out["ok"], out
    assert out["errors"][0]["code"] == code


def test_unexpected_route_resolution_error_uses_tool_envelope(adapter, monkeypatch):
    def fail_validate(_query):
        raise RuntimeError("forced route lookup failure")

    monkeypatch.setattr(adapter.runtime, "validate", fail_validate)
    out = adapter.call_tool("execute", {"query": {**QUERY, "route_decisions": ["option"]}})
    assert not out["ok"] and out["errors"][0]["code"] == "INTERNAL_ERROR", out


@pytest.mark.parametrize(
    "tool,arguments,code",
    [("unknown", {}, "UNKNOWN_MCP_TOOL"), ("inspect", {}, "INVALID_MCP_ARGUMENTS")],
)
def test_tool_checks_precede_route_resolution(adapter, monkeypatch, tool, arguments, code):
    def fail_validate(_query):
        pytest.fail("route lookup must follow tool argument checks")

    monkeypatch.setattr(adapter.runtime, "validate", fail_validate)
    out = adapter.call_tool(tool, {**arguments, "query": {**QUERY, "route_decisions": ["option"]}})
    assert not out["ok"] and out["errors"][0]["code"] == code, out


@pytest.mark.parametrize("encoded", [False, True])
def test_nested_tool_options_are_lifted(adapter, encoded):
    query = {**QUERY, "mode": "sql", "max_rows": 2, "row_format": "columns", "verbosity": "minimal"}
    actual = adapter.call_tool("execute", {"query": json.dumps(query) if encoded else query})
    assert actual["ok"] and actual["rendered_sql"], actual
    assert "rows" not in actual
    assert any("mode: lifted" in note for note in actual["normalized"])
    assert any("verbosity: lifted" in note for note in actual["normalized"])
    assert any("parsed JSON" in note for note in actual["normalized"]) is encoded


@pytest.mark.parametrize("query", ["[1]", "null", "{bad json"])
def test_invalid_encoded_query_refuses(adapter, query):
    actual = adapter.call_tool("execute", {"query": query})
    assert not actual["ok"]
    assert actual["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"


def test_conflicting_options_refuse(adapter):
    actual = adapter.call_tool("execute", {"mode": "validate", "query": {**QUERY, "mode": "run"}})
    assert not actual["ok"] and actual["errors"][0]["code"] == "INVALID_QUERY"


@pytest.mark.parametrize("mode", ["run", "sql", "validate"])
@pytest.mark.parametrize("field", ["group_by", "where"])
def test_unknown_list_dimension_is_actionable(adapter, mode, field):
    unknown = "dimension.jaffle_order_customer_ix"
    query = {"select": [], "group_by": [unknown]}
    if field == "where":
        query = {
            "select": [],
            "group_by": [DIMENSION],
            "where": [{"field": unknown, "op": "=", "value": "x"}],
        }
    actual = adapter.call_tool("execute", {"mode": mode, "query": query})
    assert not actual["ok"], actual
    issue = actual["errors"][0]
    assert issue["code"] == "OBJECT_NOT_FOUND"
    assert DIMENSION in issue["details"]["closest_matches"]


def test_encoded_policy_claims_cannot_override_transport_identity(adapter, monkeypatch):
    captured = []
    monkeypatch.setattr(adapter.runtime, "validate", lambda q: captured.append(q) or {"ok": True})
    claim = {"environment": "caller", "roles": ["admin"]}
    context = RequestContext(environment="trusted", roles=("reader",))
    actual = adapter.call_tool(
        "execute",
        {"query": json.dumps({**QUERY, "mode": "validate", "policy_context": claim})},
        request_context=context,
    )
    assert actual["ok"], actual
    assert captured[0]["policy_context"]["environment"] == "trusted"
    assert captured[0]["policy_context"]["roles"] == ["reader"]


@pytest.mark.parametrize(
    "expr",
    [
        {"kind": "arithmetic", "op": "sub", "left": MEASURE, "terms": [MEASURE, MEASURE]},
        {"kind": "arithmetic", "op": "add", "operands": [MEASURE]},
        {
            "kind": "arithmetic",
            "op": "add",
            "terms": [MEASURE, MEASURE],
            "operands": [MEASURE, MEASURE],
        },
    ],
)
def test_ambiguous_arithmetic_refuses(expr):
    with pytest.raises(SemanticLayerError, match="Arithmetic"):
        normalize_query_spellings({"select": [{"expression": expr}]}, [])


def test_literals_and_predicate_values_are_unchanged():
    value = {"op": "eq", "kind": "arithmetic", "terms": [1, 2]}
    query = {
        "where": [{"field": DIMENSION, "op": "=", "value": value}],
        "select": [{"expression": {"kind": "literal", "value": value}}],
    }
    assert normalize_query_spellings(query, []) == query


def test_top_level_query_spellings_are_normalized(adapter):
    out = adapter.call_tool(
        "execute",
        {**QUERY, "mode": "validate", "where": [{"field": DIMENSION, "op": "is_not_null"}]},
    )
    assert out["ok"] and out["normalized"], out


def test_selected_dimension_custom_alias_refuses(adapter):
    out = adapter.call_tool(
        "execute",
        {"query": {"select": [{"expression": {"dimension": DIMENSION}, "as": "customer"}]}},
    )
    assert not out["ok"] and out["errors"][0]["code"] == "INVALID_EXPRESSION_AST"


def test_live_values_retry_keeps_filters_without_host_attributes(adapter):
    query = {"where": [{"field": DIMENSION, "op": "is_not_null"}]}
    out = adapter.call_tool(
        "valid-values",
        {"dimension_id": DIMENSION, "query": query},
        request_context=RequestContext(attributes={"owner": "private-owner"}),
    )
    assert out["status"] == "needs_live_query" and not out["ok"], out
    retry = out["next_call"]["arguments"]
    assert retry["query"]["where"] == [{"field": DIMENSION, "op": "IS NOT NULL"}]
    assert retry["allow_live_query"] is True
    assert "attributes" not in retry["query"].get("policy_context", {})
    assert "private-owner" not in json.dumps(out)


def test_declared_empty_domain_is_a_successful_empty_lookup(adapter, monkeypatch):
    from dataclasses import replace

    from semantic_rails.metadata_parts import valid_values

    dimension = "dimension.jaffle_item_product_type"
    domain = valid_values._value_domain_for_dimension(adapter.runtime._config, dimension)
    monkeypatch.setattr(
        valid_values, "_value_domain_for_dimension", lambda *_: replace(domain, values=[])
    )
    out = adapter.call_tool("valid-values", {"dimension_id": dimension})
    assert out["ok"] and out["status"] == "ok", out
    assert out["values"] == [] and "next_call" not in out
