"""Per-entity value filters must never discard a dimension condition."""

from __future__ import annotations

import duckdb
import pytest
from jsonschema import Draft202012Validator

from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

DIMENSION = "dimension.jaffle_order_customer_order_number"
FIELD_FILTER = {"field": DIMENSION, "op": ">", "value": 1}


def _query(where):
    return {
        "select": [
            {
                "as": "average",
                "expression": {
                    "kind": "distribution",
                    "function": "avg",
                    "over": {
                        "kind": "entity_value",
                        "entity": "entity.jaffle_order",
                        "input": "measure.jaffle.revenue_usd",
                        "where": where,
                    },
                },
            }
        ],
    }


@pytest.fixture(scope="module")
def package_snapshot(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    return load_package_snapshot(path)


@pytest.fixture()
def engine(package_snapshot):
    runtime = Runtime.from_snapshot(package_snapshot)
    try:
        yield runtime
    finally:
        runtime.close()


# Every surface and every mode at least once.
@pytest.mark.parametrize(
    "entrypoint", ["validate", "query", "plan", "mcp-sql", "mcp-plan", "http-compile", "http-plan"]
)
def test_field_filter_is_refused_before_output(engine, monkeypatch, entrypoint):
    query = _query([{"op": ">", "value": 0}, FIELD_FILTER])

    def no_output(*args, **kwargs):
        pytest.fail("invalid entity_value.where reached compilation or the warehouse")

    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    if entrypoint in {"query", "plan"}:
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
    assert "entity_value.where[1]" in issue["message"]  # the refusal names the item's index


@pytest.mark.parametrize(
    ("where", "details"),
    [
        (
            [{**FIELD_FILTER, "kind": "value_filter"}],
            {"path": "entity_value.where[0]", "unsupported_keys": ["field"]},
        ),
        (
            [{"op": ">", "value": 1}, {"dimension": DIMENSION, "op": ">", "note": "x"}],
            {"path": "entity_value.where[1]", "unsupported_keys": ["dimension", "note"]},
        ),
        (
            [{"kind": "literal", "value": 1}],
            {"path": "entity_value.where[0]", "unsupported_keys": []},
        ),
        (["not an object"], {"path": "entity_value.where[0]", "unsupported_keys": []}),
        # Not a list at all, however falsy.
        ({}, None),
        (False, None),
        ({"op": ">", "value": 1}, None),
    ],
)
def test_unsupported_value_filters_are_refused(engine, where, details):
    response = engine.validate(_query(where))
    assert not response["ok"]
    issue = response["errors"][0]
    assert issue["code"] == "INVALID_QUERY"
    assert "entity_value.where" in issue["message"]
    assert issue.get("details") == details


@pytest.mark.parametrize(("tagged", "dimension_filter"), [(False, True), (True, False)])
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
    validator = Draft202012Validator(load_contract("query.v2.json"))
    validator.validate(
        _query([{"kind": "value_filter", "op": "gt", "value": 1}, {"op": ">", "value": 1}])
    )
    for item in (FIELD_FILTER, {"kind": "row_filter", "op": "gt", "value": 1}):
        assert list(validator.iter_errors(_query([item]))), item
    # execute's kind table writes the item's keys out.
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    table = execute["inputSchema"]["$defs"]["Expression"]["description"]
    assert "entity_value:entity*,input*:E,where:[{kind:value_filter,op:Op,value}]" in table
