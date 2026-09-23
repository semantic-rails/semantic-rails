"""MCP inspect states each fact once by default.

The inspect card repeated itself: ``object_type`` copied ``kind``,
``usage_summary`` copied the aggregation guidance beside it, ``top_values``
copied ``sample_values``, and four starter patches each repeated the select.
Its ``verbosity`` argument changed nothing. ``verbosity="minimal"`` (the MCP
default) now returns every fact once, without empty fields, with the first
starter patch; ``compact`` and ``full`` still return the whole card.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions

OBJECTS = [
    "measure.jaffle.revenue_usd",
    "metric.sales.aov_usd",
    "dimension.jaffle_store_name",
    "entity.jaffle_order",
    "segment.jaffle.high_value_customers",
]
DUPLICATES = {"object_type": "kind", "top_values": "sample_values"}
EMPTY: tuple[Any, ...] = (None, "", [], {})


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


def _without_empty(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {key: _without_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in EMPTY}
    if isinstance(value, list):
        return [_without_empty(item) for item in value]
    return value


def test_inspect_advertises_a_minimal_default() -> None:
    inspect = next(tool for tool in list_tool_definitions() if tool["name"] == "inspect")
    verbosity = inspect["inputSchema"]["properties"]["verbosity"]
    assert verbosity["default"] == "minimal"
    assert verbosity["enum"] == ["minimal", "compact", "full"]


@pytest.mark.parametrize("object_id", OBJECTS)
def test_minimal_card_keeps_every_fact_once(
    adapter: SemanticLayerMCPAdapter, object_id: str
) -> None:
    full = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": "compact"})["card"]
    slim = adapter.call_tool("inspect", {"object_id": object_id})["card"]
    for key, value in full.items():
        if key in DUPLICATES:
            assert key not in slim
            assert value in (full[DUPLICATES[key]], []), key
        elif key == "usage_summary":
            assert key not in slim
            assert all(
                slim.get(field) == _without_empty(item) or item in EMPTY
                for field, item in value.items()
            ), value
        elif key == "starter_query_patches":
            assert slim.get(key, []) == value[:1]
        elif (
            # A description that only repeats the label, a review priority
            # that repeats meta's, and empty fields are left out.
            (key == "description" and value == full["label"])
            or (key == "review_priority" and value == full.get("meta", {}).get(key))
            or _without_empty(value) in EMPTY
        ):
            assert key not in slim, key
        else:
            assert slim[key] == _without_empty(value), key
    assert set(slim) <= set(full)


def test_minimal_card_is_much_smaller(adapter: SemanticLayerMCPAdapter) -> None:
    import json

    for object_id in OBJECTS[:3]:
        full = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": "compact"})
        slim = adapter.call_tool("inspect", {"object_id": object_id})
        assert len(json.dumps(slim)) < 0.7 * len(json.dumps(full)), object_id


@pytest.mark.parametrize("verbosity", ["compact", "full"])
def test_whole_card_on_request(adapter: SemanticLayerMCPAdapter, verbosity: str) -> None:
    response = adapter.call_tool(
        "inspect", {"object_id": "measure.jaffle.revenue_usd", "verbosity": verbosity}
    )
    card = response["card"]
    assert card["object_type"] == "measure"
    assert card["usage_summary"]["default_aggregation"] == "sum"
    assert len(card["starter_query_patches"]) > 1
    assert response["verbosity"] == verbosity
