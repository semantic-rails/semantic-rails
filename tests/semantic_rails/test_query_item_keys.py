"""Query item keys cannot disappear during full or partial normalization."""

from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_partial_query, normalize_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter

EXPRESSION = {"metric": "metric.sales.orders"}
SELECT = {"expression": EXPRESSION, "as": "orders"}
FILTER = {"field": "dimension.jaffle_order_customer_id", "op": "=", "value": "customer_1"}
CHILD = {"child": "entity.jaffle_order", "match": "any", "where": [FILTER]}

CASES = [
    pytest.param(
        "select",
        {**SELECT, "expresion": EXPRESSION},
        "select[0]",
        "expresion",
        "expression",
        id="select",
    ),
    pytest.param(
        "where", {**FILTER, "feild": FILTER["field"]}, "where[0]", "feild", "field", id="where"
    ),
    pytest.param(
        "where", {**CHILD, "wher": [FILTER]}, "where[0]", "wher", "where", id="child-group"
    ),
    pytest.param(
        "where",
        {**CHILD, "where": [{**FILTER, "feild": FILTER["field"]}]},
        "where[0].where[0]",
        "feild",
        "field",
        id="child-filter",
    ),
    pytest.param(
        "metric_filters",
        {"expression": EXPRESSION, "op": ">", "value": 1, "valu": 2},
        "metric_filters[0]",
        "valu",
        "value",
        id="metric-filter",
    ),
    pytest.param(
        "metric_filters",
        {"expression": EXPRESSION, "op": ">", "value": 1, "entity": "entity.jaffle_customer"},
        "metric_filters[0]",
        "entity",
        None,
        id="metric-filter-entity",
    ),
]


@pytest.mark.parametrize("normalize", [normalize_query, normalize_partial_query])
@pytest.mark.parametrize("key,item,path,unknown,closest", CASES)
def test_unknown_query_item_keys_are_refused(normalize, key, item, path, unknown, closest):
    payload = {"select": [SELECT], key: [item]}
    with pytest.raises(SemanticLayerError) as exc:
        normalize(payload)
    error = exc.value
    assert error.code == "INVALID_QUERY"
    assert error.details["path"] == path
    assert error.details["unsupported_keys"] == [unknown]
    assert unknown not in error.details["supported_keys"]
    if closest:
        assert closest in error.details["closest_matches"]
    else:
        assert error.details["closest_matches"] == []
        hint = error.details["recovery_hints"][0]["message"]
        assert "expression" in hint and "metric_predicate" in hint and "entity" in hint


CALL_FILTER = {**FILTER, "op": "is"}
CALL_SELECT = {"expression": "metric.sales.orders", "as": "orders"}
CALL_ENTITY = {"entity": "entity.jaffle_order", "any": [CALL_FILTER]}
CALL_CASES = [
    pytest.param("select", {**CALL_SELECT, "expresion": "x"}, "select[0].expresion", id="select"),
    pytest.param("where", {**CALL_FILTER, "feild": "x"}, "where[0].feild", id="where"),
    pytest.param("where", {**CALL_ENTITY, "wher": [CALL_FILTER]}, "where[0].wher", id="entity"),
    pytest.param(
        "where",
        {**CALL_ENTITY, "any": [{**CALL_FILTER, "feild": "x"}]},
        "where[0].any[0].feild",
        id="entity-filter",
    ),
    pytest.param(
        "having",
        {"field": "orders", "op": "gt", "value": 1, "valu": 2},
        "having[0].valu",
        id="having",
    ),
    pytest.param(
        "having",
        {"field": "orders", "op": "gt", "value": 1, "entity": "entity.jaffle_customer"},
        "having[0].entity",
        id="having-entity",
    ),
]


@pytest.mark.parametrize("mode", ["run", "sql", "validate"])
@pytest.mark.parametrize("key,item,path", CALL_CASES)
def test_the_call_refuses_unknown_item_keys_with_their_path(runtime_factory, mode, key, item, path):
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        result = adapter.call_tool(
            "execute", {"mode": mode, "query": {"select": [CALL_SELECT], key: [item]}}
        )
    finally:
        adapter.close()
    assert not result["ok"]
    error = result["errors"][0]
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == path
    assert path.rsplit(".", 1)[1] not in error["details"]["supported_keys"]
    assert "rows" not in result


@pytest.mark.parametrize("normalize", [normalize_query, normalize_partial_query])
def test_all_unknown_keys_are_reported_and_item_annotations_are_refused(normalize):
    with pytest.raises(SemanticLayerError) as exc:
        normalize({"select": [{**SELECT, "_note": "ignored?", "expresion": EXPRESSION}]})
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["unsupported_keys"] == ["_note", "expresion"]
