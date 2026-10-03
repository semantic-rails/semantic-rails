"""A rank by withheld values names the reference SQL's groups, in its order, without values.

Revenue by customer at stores a and b: 103 (23), 101 (17), then 102 and 106 tie at 9 for
third place, 104 (8), 108 (2) and 105 (0: its one order has no amount). Ties are ordered by
the customer in the rank's direction, so the third place goes to 106 and ascending is the
exact reverse of descending.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.expressions import parse_config_expression
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import _tool_content
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

from .test_correctness import REVENUE, STORE, _backend

CUSTOMER = "dimension.shop_order_customer_id"
POLICY = SemanticPolicyConfig(
    id="policy.test.rank_only",
    kind="object_access",
    object_ids=[REVENUE["measure"]],
    action="withhold_values",
    roles=["sales"],
)
REFERENCE = (
    "SELECT o.customer_id FROM orders AS o WHERE o.store_id IN ('a', 'b') "
    "GROUP BY o.customer_id ORDER BY COALESCE(SUM(o.amount), 0) {0}, o.customer_id {0} LIMIT {1}"
)


@contextmanager
def _withheld(runtime: Runtime) -> Iterator[Runtime]:
    runtime._config.semantic_policies.append(POLICY)
    try:
        yield runtime
    finally:
        runtime._config.semantic_policies.remove(POLICY)


def _rank(direction: str, limit: int) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"expression": REVENUE, "as": "revenue"}],
        "group_by": [CUSTOMER],
        "where": [{"field": STORE, "op": "IN", "value": ["a", "b"]}],
        "order_by": [{"field": "revenue", "direction": direction}],
        "limit": limit,
        "policy_context": {"roles": ["sales"]},
    }


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_rank_by_withheld_values_matches_reference(request, backend_name):
    backend = _backend(request, backend_name)
    with _withheld(backend.runtimes["utc_authored"]) as runtime:
        answers = {}
        for direction in ("DESC", "ASC"):
            for limit in (3, 7):
                result = runtime.query(_rank(direction, limit))
                assert result["withheld"] == [REVENUE["measure"]]
                answers[direction, limit] = [row[CUSTOMER] for row in result["rows"]]
                assert all(set(row) == {CUSTOMER} for row in result["rows"])
                reference = backend.reference(REFERENCE.format(direction, limit))
                assert answers[direction, limit] == [row[0] for row in reference]
    assert answers["DESC", 3] == [103, 101, 106]
    assert answers["ASC", 7] == answers["DESC", 7][::-1]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("measure", [REVENUE["measure"], "measure.shop.average_order"])
@pytest.mark.parametrize("verbosity", ["minimal", "full"])
@pytest.mark.parametrize("transport", ["execute", "mcp"])
def test_null_withheld_value_has_no_data_diagnostic(
    request, backend_name, measure, verbosity, transport
):
    backend = _backend(request, backend_name)
    runtime = backend.runtimes["utc_authored"]
    policy = replace(POLICY, object_ids=[measure])
    runtime._config.semantic_policies.append(policy)
    query = {
        **_rank("DESC", 1),
        "select": [{"expression": {"measure": measure}, "as": "hidden_value"}],
        "where": [{"field": CUSTOMER, "op": "=", "value": 105}],
        "order_by": [{"field": "hidden_value", "direction": "DESC"}],
        "verbosity": verbosity,
    }
    try:
        unrestricted = runtime.query({**query, "policy_context": {}})
        assert unrestricted["rows"] == [{CUSTOMER: 105, "hidden_value": None}]
        if transport == "mcp":
            result = SemanticLayerMCPAdapter(runtime).call_tool(
                "execute", {"query": query, "mode": "run"}
            )
            serialized = json.dumps(_tool_content(result), default=str)
        else:
            result = runtime.query(query)
            serialized = json.dumps(result, default=str)
        assert result["ok"] and result["rows"] == [{CUSTOMER: 105}]
        assert result["withheld"] == [measure]
        assert "NO_DATA_IN_SCOPE" not in serialized
        assert not any(
            "hidden_value" in row.get("details", {}).get("outputs", [])
            for row in result["warnings"]
        )
        assert '"hidden_value": null' not in serialized
        assert "reads NULL rather than 0" not in serialized
        if measure == REVENUE["measure"]:
            assert any(row["code"] == "NO_DATA_IN_SCOPE" for row in unrestricted["warnings"])
    finally:
        runtime._config.semantic_policies.remove(policy)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_permitted_outputs_keep_their_no_data_diagnostic(request, backend_name):
    backend = _backend(request, backend_name)
    with _withheld(backend.runtimes["utc_authored"]) as runtime:
        query = {
            **_rank("DESC", 1),
            "select": [
                {"expression": REVENUE, "as": "revenue"},
                {"expression": {"measure": "measure.shop.tax_refunded"}, "as": "tax"},
            ],
            "where": [{"field": CUSTOMER, "op": "=", "value": 105}],
        }
        result = runtime.query(query)
        warning = next(row for row in result["warnings"] if row["code"] == "NO_DATA_IN_SCOPE")
        assert warning["details"]["outputs"] == ["tax"]
        assert warning["object_ids"] == ["measure.shop.tax_refunded"]
        assert "revenue" not in warning["message"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("explicit_ties", [False, True])
@pytest.mark.parametrize("nullable", ["value", "group_key"])
@pytest.mark.parametrize("sql_profile", ["audit", "compact"])
def test_null_rank_order_matches_reference_and_reverses(
    request, backend_name, explicit_ties, nullable, sql_profile
):
    backend = _backend(request, backend_name)
    source = backend.runtimes["utc_authored"]
    # Two a orders read 3 each, two b orders read 100 each, and the NULL store reads 6.
    expression = parse_config_expression(
        {
            "kind": "case",
            "whens": [
                {
                    "when": {
                        "kind": "comparison",
                        "op": "=",
                        "left": {"kind": "column", "column": "store_id"},
                        "right": {"kind": "literal", "value": store},
                    },
                    "then": {"kind": "literal", "value": value},
                }
                for store, value in (("a", 3), ("b", 100))
            ],
            "else": {"kind": "literal", "value": 6},
        }
    )
    config = replace(
        source._config,
        measures=[
            replace(row, expr=expression) if row.id == REVENUE["measure"] else row
            for row in source._config.measures
        ],
        semantic_policies=[POLICY],
    )
    runtime = Runtime.from_config(config, source_path=source.source_path)
    if nullable == "group_key":
        query = {
            **_rank("DESC", 10),
            "group_by": [STORE],
            "where": [{"field": "dimension.shop_order_id", "op": "IN", "value": [1, 2, 3, 8, 9]}],
        }
        reference = "SELECT store_id FROM orders WHERE order_id IN (1, 2, 3, 8, 9) GROUP BY store_id ORDER BY (SUM(CASE WHEN store_id = 'b' THEN 100 WHEN store_id = 'a' THEN 3 ELSE 6 END) IS NULL), SUM(CASE WHEN store_id = 'b' THEN 100 WHEN store_id = 'a' THEN 3 ELSE 6 END) DESC, (store_id IS NULL), store_id DESC"
        key = STORE
    else:
        query = {
            **_rank("DESC", 10),
            "select": [{"expression": {"measure": "measure.shop.average_order"}, "as": "revenue"}],
            "where": [],
        }
        runtime._config.semantic_policies[:] = [
            replace(POLICY, object_ids=["measure.shop.average_order"])
        ]
        reference = "SELECT customer_id FROM orders GROUP BY customer_id ORDER BY (AVG(amount) IS NULL), AVG(amount) DESC, (customer_id IS NULL), customer_id DESC"
        key = CUSTOMER
    try:
        answers = {}
        for direction in ("DESC", "ASC"):
            order = [{"field": "revenue", "direction": direction}]
            if explicit_ties:
                order.append({"field": key, "direction": direction})
            result = runtime.query({**query, "order_by": order, "sql_profile": sql_profile})
            answers[direction] = [row[key] for row in result["rows"]]
        assert answers["DESC"] == [row[0] for row in backend.reference(reference)]
        assert answers["ASC"] == answers["DESC"][::-1]
        if nullable == "group_key":
            assert answers["DESC"] == ["b", "a", None]
            values = runtime.query({**query, "policy_context": {}})["rows"]
            assert {row[STORE]: row["revenue"] for row in values} == {"a": 6, None: 6, "b": 200}
        else:
            assert answers["DESC"][-1] == 105
    finally:
        runtime.close()
