"""Spelling normalization preserves canonical results and rejects conflicting input."""

from __future__ import annotations

import json

import duckdb
import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_query import normalize_query_spellings
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
        *[
            {"expression": {"kind": kind, "dimension": DIMENSION}}
            for kind in ("dimension", "group", "ref")
        ],
    ],
)
def test_selected_dimension_moves_to_group_by(adapter, item):
    canonical = adapter.call_tool(
        "execute",
        {"query": {"select": [], "group_by": [DIMENSION], "order_by": [{"field": DIMENSION}]}},
    )
    actual = adapter.call_tool(
        "execute", {"query": {"select": [item], "order_by": [{"field": DIMENSION}]}}
    )
    assert actual["ok"] and canonical["ok"], actual
    assert actual["rows"] == canonical["rows"]
    assert "moved" in actual["normalized"][0]


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
    assert not out["ok"] and out["errors"][0]["code"] == "INVALID_QUERY"


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
