"""Dry runs, recovery hints and disclosures agree with the executable call."""

from __future__ import annotations

import json

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime

ORDERS = "measure.jaffle.order_count"
REVENUE = "measure.jaffle.revenue_usd"
STORE = "dimension.jaffle_store_name"
CUSTOMER = "dimension.jaffle_order_customer_id"
SHARE = {"kind": "share", "input": REVENUE, "over": [STORE]}


@pytest.fixture
def runtime(runtime_factory):
    rt = runtime_factory("jaffle_shop")
    yield rt
    rt.close()


@pytest.mark.parametrize("mode", ["validate", "sql", "run"])
@pytest.mark.parametrize("warm", [False, True])
def test_missing_path_has_the_same_diagnostic_in_every_mode(runtime, mode, warm):
    adapter = SemanticLayerMCPAdapter(runtime)
    if warm:
        assert runtime.validate({"select": ["metric.sales.inventory_on_hand_eop"]})["ok"]
    query = {
        "select": ["metric.sales.inventory_on_hand_eop"],
        "group_by": ["dimension.jaffle_customer_type"],
    }
    expected = adapter.call_tool("execute", {"query": query, "mode": "run"})
    actual = adapter.call_tool("execute", {"query": query, "mode": mode})
    assert not actual["ok"]
    assert actual["errors"][0]["code"] == "PATH_NOT_FOUND"
    assert actual["errors"][0]["details"] == expected["errors"][0]["details"]


def _code_filter(surface, op, value):
    condition = {"field": CUSTOMER, "op": op, "value": value}
    if surface == "where":
        return {"select": [ORDERS], "where": [condition]}
    if surface == "entity":
        return {
            "select": [ORDERS],
            "where": [{"entity": "entity.jaffle_order", "any": [condition]}],
        }
    if surface == "aggregate":
        expr = {"kind": "aggregate", "measure": ORDERS, "filter": {"all": [condition]}}
    elif surface == "scoped":
        expr = {"kind": "scoped_aggregate", "measure": ORDERS, "where": [condition]}
    else:
        expr = {
            "kind": "aggregate_if",
            "aggregation": "count",
            "condition": {
                "kind": "comparison",
                "op": op,
                "left": {
                    "kind": "column",
                    "entity": "entity.jaffle_order",
                    "column": "customer_id",
                },
                "right": {"kind": "literal", "value": value},
            },
        }
    return {"select": [{"expression": expr, "as": "n"}]}


@pytest.mark.parametrize("surface", ["where", "entity", "aggregate", "scoped", "comparison"])
@pytest.mark.parametrize("op", ["LIKE", "NOT LIKE", "ILIKE"])
def test_like_on_an_id_hints_exact_values_instead_of_an_unusable_text_word(runtime, surface, op):
    result = runtime.validate(_code_filter(surface, op, "%1"))
    assert not result["ok"]
    issue = result["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    hint = issue["recovery_hints"][0]["message"]
    assert "exact values" in hint
    assert "ends_with" not in hint
    assert "not" in hint if op == "NOT LIKE" else "is" in hint
    # The suggested exact-value filter is accepted on this same surface.
    fixed = _code_filter(surface, "not" if op == "NOT LIKE" else "is", "customer_1")
    assert runtime.validate(fixed)["ok"]


@pytest.mark.parametrize("mode", ["validate", "sql", "run"])
@pytest.mark.parametrize("wrapper", ["round", "arithmetic", "two-shares"])
def test_nested_shares_keep_each_denominator_disclosure(runtime, mode, wrapper):
    if wrapper == "round":
        expr = {"kind": "call", "name": "round", "args": [SHARE, {"kind": "literal", "value": 4}]}
    else:
        expr = {
            "kind": "arithmetic",
            "op": "multiply",
            "left": SHARE,
            "right": SHARE if wrapper == "two-shares" else {"kind": "literal", "value": 100},
        }
    result = SemanticLayerMCPAdapter(runtime).call_tool(
        "execute",
        {
            "mode": mode,
            "query": {"select": [{"expression": expr, "as": "value"}], "group_by": [STORE]},
        },
    )
    assert result["ok"], result
    shares = [row for row in result["assumptions"] if row["code"] == "SHARE_OF_TOTAL"]
    assert len(shares) == (2 if wrapper == "two-shares" else 1)
    for share in shares:
        assert (share["column"], share["input"], share["over"]) == ("value", REVENUE, [STORE])
        assert "share" in share["message"].lower()
        assert "value is a fraction" not in share["message"]
    if mode == "run":
        bare = runtime.query(
            {"select": [{"expression": SHARE, "as": "share"}], "group_by": [STORE]}
        )
        expected = {row[STORE]: row["share"] for row in bare["rows"]}
        for row in result["rows"]:
            fraction = expected[row[STORE]]
            value = (
                round(fraction, 4)
                if wrapper == "round"
                else fraction * (fraction if wrapper == "two-shares" else 100)
            )
            assert row["value"] == pytest.approx(value)


@pytest.mark.parametrize(
    "field", ["unreturned_total", {"kind": "call", "name": "round", "args": [REVENUE]}]
)
def test_unreturned_having_expression_explains_how_to_select_it(runtime, field):
    result = runtime.validate(
        {"select": [ORDERS], "having": [{"field": field, "op": "gt", "value": 0}]}
    )
    assert not result["ok"]
    issue = result["errors"][0]
    assert (issue["code"], issue["details"]["path"]) == ("INVALID_QUERY", "having[0].field")
    hint = issue["recovery_hints"][0]["message"]
    assert "select" in hint and "alias" in hint
    assert json.dumps("having") in hint
    fixed = {
        "select": [
            ORDERS,
            {"expression": {"kind": "call", "name": "round", "args": [REVENUE]}, "as": "total"},
        ],
        "having": [{"field": "total", "op": "gt", "value": 0}],
    }
    assert runtime.validate(fixed)["ok"]
