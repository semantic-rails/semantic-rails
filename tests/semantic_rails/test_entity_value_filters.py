"""Per-entity value filters must never discard a dimension condition."""

from __future__ import annotations

import duckdb
import pytest
from jsonschema import Draft202012Validator

from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan_payload

DIMENSION = "dimension.jaffle_order_customer_order_number"
FIELD_FILTER = {"field": DIMENSION, "op": ">", "value": 1}


def _query(where):
    return {
        "version": 1,
        "select": [
            {
                "as": "average",
                "expression": {
                    "kind": "distribution",
                    "function": "avg",
                    "over": {
                        "kind": "entity_value",
                        "entity": "entity.jaffle_order",
                        "input": {"measure": "measure.jaffle.revenue_usd"},
                        "where": where,
                    },
                },
            }
        ],
    }


@pytest.fixture()
def engine(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


@pytest.mark.parametrize(
    "entrypoint",
    [
        "validate",
        "compile",
        "query",
        "plan",
        "mcp-run",
        "mcp-validate",
        "mcp-sql",
        "mcp-plan",
        "http-validate",
        "http-compile",
        "http-query",
        "http-plan",
    ],
)
def test_field_filter_is_refused_before_output(engine, monkeypatch, entrypoint):
    query = _query([{"op": ">", "value": 0}, FIELD_FILTER])

    def no_output(*args, **kwargs):
        pytest.fail("invalid entity_value.where reached compilation or the warehouse")

    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    if entrypoint in {"compile", "query", "plan"}:
        with pytest.raises(SemanticLayerError) as exc:
            if entrypoint == "plan":
                plan_payload(engine, intent="revenue", partial_query=query)
            else:
                getattr(engine, entrypoint)(query)
        issue = {"code": exc.value.code, "message": str(exc.value)}
    elif entrypoint == "validate":
        response = engine.validate(query)
        assert not response["ok"]
        issue = response["errors"][0]
    elif entrypoint.startswith("mcp-"):
        mode = entrypoint.removeprefix("mcp-")
        adapter = SemanticLayerMCPAdapter(engine)
        response = (
            adapter.call_tool("plan", {"intent": "revenue", "query": query})
            if mode == "plan"
            else adapter.call_tool("execute", {"query": query, "mode": mode})
        )
        assert not response["ok"]
        issue = response["error"]
    else:
        operation = entrypoint.removeprefix("http-")
        service = SemanticHTTPService(engine)
        body = {"intent": "revenue", "query": query} if operation == "plan" else query
        try:
            response, status = service.handle("POST", normalize_route(f"/api/v1/{operation}"), body)
        except SemanticLayerError as exc:
            response, status = service.exception_payload(exc, stage="http")
        assert not response["ok"]
        issue = response.get("error") or response["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert "entity_value.where[1]" in issue["message"]
    assert "top-level 'where'" in issue["message"]


@pytest.mark.parametrize(
    "item",
    [
        {**FIELD_FILTER, "kind": "value_filter"},
        {"field": None},
        {"dimension": DIMENSION, "op": ">", "value": 1},
        {"expression": {"measure": "measure.jaffle.revenue_usd"}, "value": 1},
        {"op": ">", "value": 1, "note": "ignored"},
        {"kind": "literal", "value": 1},
        "not an object",
    ],
)
def test_unsupported_value_filter_items_are_refused(engine, item):
    response = engine.validate(_query([item]))
    assert not response["ok"]
    issue = response["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert "entity_value.where[0]" in issue["message"]


@pytest.mark.parametrize("where", [{}, False, "", {"op": ">", "value": 1}])
def test_value_filters_require_a_list(engine, where):
    response = engine.validate(_query(where))
    assert not response["ok"]
    issue = response["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert "entity_value.where must be a list" in issue["message"]


@pytest.mark.parametrize("tagged", [False, True])
@pytest.mark.parametrize("dimension_filter", [False, True])
def test_valid_value_filter_matches_reference_sql(engine, tagged, dimension_filter):
    item = {"op": ">", "value": 1}
    if tagged:
        item["kind"] = "value_filter"
    query = _query([item])
    if dimension_filter:
        query["where"] = [FIELD_FILTER]
    actual = engine.query(query)["rows"][0]["average"]
    sql = "SELECT AVG(order_total_cents / 100.0) FROM jaffle_order WHERE order_total_cents / 100.0 > 1"
    if dimension_filter:
        sql += " AND customer_order_number > 1"
    with duckdb.connect(engine.db_path, read_only=True) as db:
        reference = db.execute(sql).fetchone()[0]
    assert actual == pytest.approx(reference)


def test_published_schema_closes_value_filter_items():
    validator = Draft202012Validator(load_contract("query_ir.v1.json"))
    valid = _query([{"kind": "value_filter", "op": ">", "value": 1}])
    validator.validate(valid)
    invalid = _query([FIELD_FILTER])
    assert list(validator.iter_errors(invalid))
