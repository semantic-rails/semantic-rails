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


@pytest.mark.parametrize("mode", ["run", "sql", "validate"])
@pytest.mark.parametrize("key,item,path,unknown,closest", CASES)
def test_mcp_spelling_rewrites_cannot_drop_unknown_item_keys(
    runtime_factory, mode, key, item, path, unknown, closest
):
    # An operator spelling rewrite must preserve the unknown key for the central guard.
    item = {**item, **({"op": "gt"} if key == "metric_filters" else {})}
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        result = adapter.call_tool(
            "execute", {"mode": mode, "query": {"select": [SELECT], key: [item]}}
        )
    finally:
        adapter.close()
    assert not result["ok"]
    error = result["errors"][0]
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == path
    assert error["details"]["unsupported_keys"] == [unknown]
    assert "rows" not in result


@pytest.mark.parametrize("normalize", [normalize_query, normalize_partial_query])
def test_all_unknown_keys_are_reported_and_item_annotations_are_refused(normalize):
    with pytest.raises(SemanticLayerError) as exc:
        normalize({"select": [{**SELECT, "_note": "ignored?", "expresion": EXPRESSION}]})
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["unsupported_keys"] == ["_note", "expresion"]
