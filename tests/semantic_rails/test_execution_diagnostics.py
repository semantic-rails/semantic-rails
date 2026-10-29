"""Dry runs, recovery hints and disclosures agree with the executable call."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime
from semantic_rails.runtime_parts.assumptions import share_of_total

ORDERS = "measure.jaffle.order_count"
REVENUE = "measure.jaffle.revenue_usd"
STORE = "dimension.jaffle_store_name"
CUSTOMER = "dimension.jaffle_order_customer_id"
SHARE = {"kind": "share", "input": REVENUE, "over": [STORE]}


@pytest.fixture
def runtime(runtime_factory) -> Iterator[Runtime]:
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
        condition["field"] = "dimension.jaffle_item_order_id"
        return {
            "select": [ORDERS],
            "where": [{"entity": "entity.jaffle_item", "any": [condition]}],
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
                "op": {"is": "=", "not": "<>"}.get(op, op),
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
def test_like_on_an_id_hints_the_text_word_that_reads_it_as_text(runtime, surface, op):
    result = runtime.validate(_code_filter(surface, op, "%1"))
    assert not result["ok"]
    issue = result["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    hint = issue["recovery_hints"][0]["message"]
    word = "not_ends_with" if op == "NOT LIKE" else "ends_with"
    operand = "right" if surface == "comparison" else "value"
    assert hint.startswith(f'Write {{"op": "{word}", "{operand}": '), hint
    assert "exact values" not in hint
    # The hinted text word is accepted on this same surface: it reads the id as its text.
    accepted = runtime.validate(_code_filter(surface, word, "1"))
    assert accepted["ok"], accepted


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
        assert not share["message"].startswith("value is a fraction")
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


def test_having_expression_key_also_explains_how_to_select_it(runtime):
    result = runtime.validate(
        {
            "select": [ORDERS],
            "having": [
                {
                    "expression": {"kind": "call", "name": "round", "args": [REVENUE]},
                    "op": "gt",
                    "value": 0,
                }
            ],
        }
    )
    assert not result["ok"]
    issue = result["errors"][0]
    assert (issue["code"], issue["details"]["path"]) == ("INVALID_QUERY", "having[0].expression")
    assert '"select" with an alias' in issue["recovery_hints"][0]["message"]


def test_a_measure_only_used_by_having_stays_unreturned(runtime):
    result = runtime.query(
        {
            "select": [ORDERS],
            "group_by": [STORE],
            "having": [{"field": REVENUE, "op": "gte", "value": 300000}],
        }
    )
    reference = runtime._get_adapter().query(
        "SELECT s.store_name AS store, COUNT(DISTINCT o.order_id) AS n FROM jaffle_order o LEFT JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY 1 HAVING SUM(o.order_total_cents / 100.0) >= 300000"
    )
    assert result["rows"] == [{STORE: row["store"], "order_count": row["n"]} for row in reference]


def test_nested_share_disclosure_survives_empty_results(runtime):
    expression = {
        "kind": "arithmetic",
        "op": "multiply",
        "left": SHARE,
        "right": {"kind": "literal", "value": 100},
    }
    result = runtime.query(
        {
            "select": [{"expression": expression, "as": "percent"}],
            "group_by": [STORE],
            "where": [{"field": STORE, "op": "is", "value": "Unknown store"}],
        }
    )
    assert result["rows"] == []
    [share] = [row for row in result["assumptions"] if row["code"] == "SHARE_OF_TOTAL"]
    assert share["input"] == REVENUE
    assert "rows" not in share


def test_share_disclosures_keep_distinct_inputs_and_skip_literal_data():
    expression = {
        "kind": "call",
        "name": "coalesce",
        "args": [
            SHARE,
            {"kind": "share", "input": {"measure": ORDERS}, "over": ["period"]},
            {"kind": "literal", "value": SHARE},
        ],
    }
    # A compiled share's input is an expression, not the call's reference string.
    expression["args"][0] = {**SHARE, "input": {"measure": REVENUE}}
    compiled = {
        "logical_plan": SimpleNamespace(
            query={"select": [{"expression": expression, "as": "value"}]}
        ),
        "share_groups": {"value": "s1_groups"},
    }
    shares = share_of_total(compiled, {"s1_groups": [2]})
    assert [(row["input"], row["over"]) for row in shares] == [
        (REVENUE, [STORE]),
        (ORDERS, ["period"]),
    ]
    assert all("rows" not in row for row in shares)
