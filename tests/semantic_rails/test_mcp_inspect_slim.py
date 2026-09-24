"""MCP inspect states each fact once on explicit minimal requests.

The inspect card repeated itself: ``object_type`` copied ``kind``,
``usage_summary`` copied the aggregation guidance beside it, ``top_values``
copied ``sample_values``, and four starter patches each repeated the select.
Its ``verbosity`` argument changed nothing. Explicit ``verbosity="minimal"``
now returns every fact once, without empty fields, with the first starter
patch; omitted, ``compact`` and ``full`` keep the v1 whole card.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions
from semantic_rails.metadata import _slim_inspect_card
from semantic_rails.runtime import Runtime

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


def test_inspect_advertises_v1_default_and_minimal_opt_in() -> None:
    inspect = next(tool for tool in list_tool_definitions() if tool["name"] == "inspect")
    verbosity = inspect["inputSchema"]["properties"]["verbosity"]
    assert verbosity["default"] == "compact"
    assert verbosity["enum"] == ["minimal", "compact", "full"]


@pytest.mark.parametrize("object_id", OBJECTS)
def test_minimal_card_keeps_every_fact_once(
    adapter: SemanticLayerMCPAdapter, object_id: str
) -> None:
    full = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": "compact"})["card"]
    slim = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": "minimal"})["card"]
    for key, value in full.items():
        if key in DUPLICATES:
            assert key not in slim
            assert value in (full[DUPLICATES[key]], []), key
        elif key == "usage_summary":
            assert key not in slim
            assert all(slim.get(field) == item or item in EMPTY for field, item in value.items()), (
                value
            )
        elif key == "starter_query_patches":
            assert slim.get(key, []) == value[:1]
        elif key == "sample_values":
            assert len(slim[key]) == len(value)
            for original_row, slim_row in zip(value, slim[key], strict=True):
                assert "value" in slim_row
                assert slim_row["value"] == original_row["value"]
                for field, item in original_row.items():
                    if field == "value" or item not in EMPTY:
                        assert slim_row[field] == item
                    else:
                        assert field not in slim_row
        elif key == "accumulation":
            assert slim[key]["kind"] == value["kind"]
            assert "snapshot" not in slim[key]
        elif key == "policy_effects":
            assert len(slim[key]) == len(value)
            for original_effect, slim_effect in zip(value, slim[key], strict=True):
                for field, item in original_effect.items():
                    if item in EMPTY:
                        assert field not in slim_effect
                    else:
                        assert slim_effect[field] == item
        elif (
            # A description that only repeats the label, a review priority
            # that repeats meta's, and empty fields are left out.
            (key == "description" and value == full["label"])
            or (key == "review_priority" and value == full.get("meta", {}).get(key))
            or value in EMPTY
        ):
            assert key not in slim, key
        else:
            assert slim[key] == value, key
    assert set(slim) <= set(full)


def test_minimal_inspect_preserves_declared_raw_values(
    package_config_factory: Any,
) -> None:
    _, package_dir = package_config_factory("jaffle_shop")
    model_file = Path(package_dir) / "models" / "core" / "order_items.yml"
    model = yaml.safe_load(model_file.read_text(encoding="utf-8"))
    declared = [
        {"value": "", "label": "Blank"},
        {"value": None, "label": "Unknown"},
        {"value": 0, "label": "Zero"},
        {"value": False, "label": "False"},
        {"value": {"nested": ["", None, 0, False]}, "label": "Structured"},
    ]
    model["model"]["dimensions"]["product_type"]["domain"] = declared
    model_file.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    mcp = SemanticLayerMCPAdapter(Runtime.from_path(str(package_dir)))
    try:
        arguments = {"object_id": "dimension.jaffle_item_product_type"}
        full = mcp.call_tool("inspect", {**arguments, "verbosity": "compact"})
        minimal = mcp.call_tool("inspect", {**arguments, "verbosity": "minimal"})
        assert full["ok"] is minimal["ok"] is True
        full_values = full["card"]["sample_values"]
        minimal_values = minimal["card"]["sample_values"]
        assert len(minimal_values) == len(declared) == 5
        for row, expected in zip(minimal_values, declared, strict=True):
            assert "value" in row
            assert type(row["value"]) is type(expected["value"])
            assert row["value"] == expected["value"]
            assert row["label"] == expected["label"]
        for original_row, slim_row in zip(full_values, minimal_values, strict=True):
            assert slim_row["value"] == original_row["value"]
    finally:
        mcp.close()


@pytest.mark.parametrize("value", ["", None, 0, False, {"nested": ["", None, 0, False]}])
def test_minimal_inspect_preserves_starter_query_literals(
    adapter: SemanticLayerMCPAdapter, value: Any
) -> None:
    query = {
        "version": 2,
        "where": [{"field": "dimension.jaffle_store_name", "op": "eq", "value": value}],
    }
    response = adapter.call_tool(
        "inspect",
        {"object_id": "measure.jaffle.revenue_usd", "query": query, "verbosity": "minimal"},
    )
    assert response["ok"] is True
    filter_row = response["card"]["starter_query_patches"][0]["query_patch"]["where"][0]
    assert "value" in filter_row
    assert type(filter_row["value"]) is type(value)
    assert filter_row["value"] == value


def test_minimal_inspect_preserves_nested_constraint_and_example_payloads() -> None:
    query = {"where": [{"field": "dimension.example", "op": "eq", "value": None}]}
    card = {
        "id": "measure.example",
        "kind": "measure",
        "label": "Example",
        "policy_effects": [
            {
                "policy_id": "policy.example",
                "kind": "metric_constraint",
                "action": "constrain",
                "rationale": "",
                "constraints": {"allowed_where": [], "allow_metric_filters": False},
            }
        ],
        "example_entries": [{"query": query}],
        "starter_query_patches": [{"kind": "select", "query_patch": query}],
    }
    slim = _slim_inspect_card(card)
    effect = slim["policy_effects"][0]
    assert effect["constraints"] == {"allowed_where": [], "allow_metric_filters": False}
    assert "rationale" not in effect
    assert slim["example_entries"][0]["query"] == query
    assert slim["starter_query_patches"][0]["query_patch"] == query


def test_minimal_card_is_much_smaller(adapter: SemanticLayerMCPAdapter) -> None:
    import json

    for object_id in OBJECTS[:3]:
        full = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": "compact"})
        slim = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": "minimal"})
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


def test_omitted_inspect_verbosity_keeps_v1_whole_card(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    object_id = "measure.jaffle.revenue_usd"
    default = adapter.call_tool("inspect", {"object_id": object_id})
    compact = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": "compact"})
    assert default["card"] == compact["card"]
    assert default["verbosity"] == "compact"
    assert {"object_type", "usage_summary", "starter_query_patches"} <= set(default["card"])
