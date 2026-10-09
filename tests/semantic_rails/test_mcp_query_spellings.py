"""The MCP tools read one spelling of the call: aliases are the operator table's, nothing else."""

from __future__ import annotations

import json

import duckdb
import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.request_context import RequestContext

MEASURE = "measure.jaffle.order_count"
DIMENSION = "dimension.jaffle_order_customer_id"
QUERY = {"select": [{"expression": MEASURE, "as": "n"}]}


@pytest.fixture
def adapter(runtime_factory):
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    yield adapter
    adapter.close()


@pytest.mark.parametrize(
    "alias,word",
    [
        ("eq", "is"),
        ("==", "is"),
        ("neq", "not"),
        ("<>", "not"),
        (">", "gt"),
        (">=", "gte"),
        ("<", "lt"),
        ("<=", "lte"),
    ],
)
def test_operator_aliases_answer_as_their_word(adapter, alias, word):
    predicate = {"field": DIMENSION, "op": alias, "value": "customer_1"}
    expected = adapter.call_tool(
        "execute", {"query": {**QUERY, "where": [{**predicate, "op": word}]}}
    )
    actual = adapter.call_tool("execute", {"query": {**QUERY, "where": [predicate]}})
    assert actual["ok"] and expected["ok"], actual
    assert actual["rows"] == expected["rows"]
    assert "normalized" not in actual


@pytest.mark.parametrize(
    "spelling,hint",
    [("equals", "Filters use the words"), ("is_not_null", '"op": "missing", "value": false')],
)
def test_spellings_outside_the_operator_table_are_refused(adapter, spelling, hint):
    query = {**QUERY, "where": [{"field": DIMENSION, "op": spelling, "value": "customer_1"}]}
    out = adapter.call_tool("execute", {"query": query, "mode": "validate"})
    assert not out["ok"], out
    issue = out["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert issue["details"]["path"] == "where[0].op"
    assert hint in issue["recovery_hints"][0]["message"]


@pytest.mark.parametrize("op,sql_op", [("add", "+"), ("subtract", "-"), ("multiply", "*")])
def test_arithmetic_matches_reference_sql(adapter, op, sql_op):
    expr = {
        "kind": "arithmetic",
        "op": op,
        "left": MEASURE,
        "right": {"kind": "literal", "value": 2},
    }
    actual = adapter.call_tool("execute", {"query": {"select": [{"expression": expr, "as": "n"}]}})
    assert actual["ok"], actual
    with duckdb.connect(adapter.runtime.db_path, read_only=True) as db:
        expected = db.execute(
            f"SELECT COUNT(DISTINCT order_id) {sql_op} 2 FROM jaffle_order"
        ).fetchone()[0]
    assert actual["rows"][0]["n"] == pytest.approx(expected)


@pytest.mark.parametrize("shape", ["operands", "terms"])
def test_arithmetic_takes_left_and_right_only(adapter, shape):
    expr = {"kind": "arithmetic", "op": "add", shape: [MEASURE, MEASURE]}
    out = adapter.call_tool(
        "execute", {"query": {"select": [{"expression": expr, "as": "n"}]}, "mode": "validate"}
    )
    assert not out["ok"], out
    issue = out["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert issue["details"]["path"] == f"select[0].expression.{shape}"


@pytest.mark.parametrize("mode", ["run", "sql", "validate"])
@pytest.mark.parametrize("surface", ["mcp", "runtime"])
def test_selected_dimension_moves_to_group_by(adapter, mode, surface):
    canonical = adapter.call_tool(
        "execute",
        {"query": {"group_by": [DIMENSION], "order_by": [{"field": DIMENSION}], "limit": 20}},
    )
    query = {"select": [DIMENSION], "order_by": [{"field": DIMENSION}], "limit": 20}
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


@pytest.mark.parametrize(
    "item,path",
    [
        ({"dimension": DIMENSION}, "select[0].dimension"),
        ({"expression": {"dimension": DIMENSION}}, "select[0].expression"),
        (
            {"expression": {"kind": "dimension", "dimension": DIMENSION}},
            "select[0].expression.kind",
        ),
        ({"expression": {"kind": "ref", "dimension": DIMENSION}}, "select[0].expression.kind"),
    ],
)
def test_dimension_objects_in_select_are_refused_with_the_string_form(adapter, item, path):
    out = adapter.call_tool("execute", {"mode": "validate", "query": {"select": [item]}})
    assert not out["ok"], out
    issue = out["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert issue["details"]["path"] == path
    assert issue["recovery_hints"], issue


def test_a_selected_dimension_beside_another_group_by_is_refused(adapter):
    query = {"select": [DIMENSION], "group_by": ["dimension.jaffle_order_store_id"]}
    out = adapter.call_tool("execute", {"mode": "validate", "query": query})
    assert not out["ok"], out
    issue = out["errors"][0]
    assert issue["details"]["path"] == "select[0]"
    assert f"Add '{DIMENSION}' to group_by" in issue["recovery_hints"][0]["message"]


@pytest.mark.parametrize("transport", ["direct", "jsonrpc"])
def test_deep_expression_input_is_a_structured_refusal(adapter, transport):
    nested = MEASURE
    for _ in range(994):
        nested = {
            "kind": "arithmetic",
            "op": "add",
            "left": nested,
            "right": {"kind": "literal", "value": 1},
        }
    arguments = {"mode": "validate", "query": {"select": [{"expression": nested}]}}
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
    assert out["errors"][0]["code"] == "INVALID_QUERY"


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("option", ["mode", "max_rows", "row_format", "verbosity", "now"])
def test_a_tool_argument_inside_the_query_is_refused(adapter, encoded, option):
    query = {**QUERY, option: "sql" if option == "mode" else 2}
    actual = adapter.call_tool("execute", {"query": json.dumps(query) if encoded else query})
    assert not actual["ok"], actual
    issue = actual["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert issue["details"]["path"] == option
    assert issue["recovery_hints"][0]["message"] == f"Pass `{option}` beside `query`."


def test_a_query_sent_as_json_text_is_read_as_the_call(adapter):
    as_text = adapter.call_tool("execute", {"query": json.dumps(QUERY)})
    as_object = adapter.call_tool("execute", {"query": QUERY})
    assert as_text["ok"] and as_text["rows"] == as_object["rows"], as_text


@pytest.mark.parametrize("query", ["[1]", "null", "{bad json"])
def test_invalid_encoded_query_refuses(adapter, query):
    actual = adapter.call_tool("execute", {"query": query})
    assert not actual["ok"]
    assert actual["errors"][0]["code"] == "INVALID_MCP_ARGUMENTS"


def test_a_call_sent_as_execute_arguments_is_refused(adapter):
    actual = adapter.call_tool("execute", {**QUERY, "mode": "validate"})
    assert not actual["ok"], actual
    issue = actual["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert issue["details"]["path"] == "select"
    assert '{"query": {"select": ...}}' in issue["recovery_hints"][0]["message"]


@pytest.mark.parametrize("remote", [False, True])
def test_an_encoded_identity_claim_is_refused(adapter, remote):
    claim = {"environment": "caller", "roles": ["admin"]}
    actual = adapter.call_tool(
        "execute",
        {"query": json.dumps({**QUERY, "policy_context": claim})},
        request_context=RequestContext(environment="trusted") if remote else None,
    )
    assert not actual["ok"], actual
    issue = actual["errors"][0]
    assert issue["code"] == ("IDENTITY_CLAIM_REFUSED" if remote else "INVALID_QUERY")
    assert issue["details"]["path"] == "query.policy_context"


@pytest.mark.parametrize("mode", ["run", "sql", "validate"])
@pytest.mark.parametrize("field", ["group_by", "where"])
def test_unknown_list_dimension_is_actionable(adapter, mode, field):
    unknown = "dimension.jaffle_order_customer_ix"
    query = {"group_by": [unknown]}
    if field == "where":
        query = {"group_by": [DIMENSION], "where": [{"field": unknown, "op": "is", "value": "x"}]}
    actual = adapter.call_tool("execute", {"mode": mode, "query": query})
    assert not actual["ok"], actual
    issue = actual["errors"][0]
    assert issue["code"] == "OBJECT_NOT_FOUND"
    assert DIMENSION in issue["details"]["closest_matches"]


def test_live_values_retry_keeps_the_callers_call(adapter):
    query = {"where": [{"field": DIMENSION, "op": "missing", "value": False}]}
    out = adapter.call_tool(
        "valid-values",
        {"dimension_id": DIMENSION, "query": query},
        request_context=RequestContext(attributes={"owner": "private-owner"}),
    )
    assert out["status"] == "needs_live_query" and not out["ok"], out
    retry = out["next_call"]["arguments"]
    assert retry["query"] == query
    assert retry["allow_live_query"] is True
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
