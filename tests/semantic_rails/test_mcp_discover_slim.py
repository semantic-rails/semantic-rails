"""MCP discover offers slim cards explicitly, and canonical objects rank first.

Full cards (match reasons, starter patches, comparison metadata) made
discover over half of a typical agent session's context. Explicit
verbosity="minimal" returns five slim cards per kind; omitted arguments keep
the v1 full cards and count. When the question names an object outright ("revenue by
store"), that object outranks near-duplicates that add a qualifier the question
never used ("delivered revenue", "drink revenue").
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions

SLIM_KEYS = {"id", "kind", "label", "score", "description", "default_temporal_role", "available"}
VALUE_KEYS = {"id", "kind", "dimension_id", "value", "label", "available", "score"}
SESSION_STARTS = {
    "version": 2,
    "select": [{"as": "s", "expression": {"measure": "measure.jaffle.session_starts"}}],
}


@pytest.fixture()
def adapter(runtime_factory: Any) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


def test_discover_advertises_v1_defaults_and_slim_opt_in() -> None:
    discover = next(tool for tool in list_tool_definitions() if tool["name"] == "discover")
    properties = discover["inputSchema"]["properties"]
    assert properties["verbosity"]["default"] == "compact"
    assert properties["verbosity"]["enum"] == ["minimal", "compact", "full"]
    assert properties["limit"]["default"] == 10


def test_explicit_minimal_discover_returns_five_slim_cards_per_kind(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    response = adapter.call_tool(
        "discover", {"terms": "revenue by store", "verbosity": "minimal", "limit": 5}
    )
    assert response["ok"], response["errors"]
    for bucket in ("measures", "metrics", "dimensions", "entities"):
        rows = response[bucket]
        assert 0 < len(rows) <= 5, bucket
        for row in rows:
            assert set(row) <= SLIM_KEYS, (bucket, sorted(set(row) - SLIM_KEYS))
    for row in response["dimension_values"]:
        assert set(row) <= VALUE_KEYS | {"blocked_reason"}


@pytest.mark.parametrize(
    ("terms", "value", "label"),
    [("Food", "jaffle", "Food"), ("Drink", "beverage", "Drink")],
)
def test_minimal_dimension_value_keeps_business_label_and_availability(
    adapter: SemanticLayerMCPAdapter, terms: str, value: str, label: str
) -> None:
    arguments = {"terms": terms, "limit": 5}
    full = adapter.call_tool("discover", {**arguments, "verbosity": "compact"})
    minimal = adapter.call_tool("discover", {**arguments, "verbosity": "minimal"})
    expected_id = f"dimension.jaffle_item_product_type={value}"
    full_value = next(row for row in full["dimension_values"] if row["id"] == expected_id)
    slim_value = next(row for row in minimal["dimension_values"] if row["id"] == expected_id)
    assert full_value["label"] == slim_value["label"] == label
    assert full_value["value"] == slim_value["value"] == value
    assert full_value["available"] is slim_value["available"] is True
    assert set(slim_value) <= VALUE_KEYS


def test_minimal_blocked_dimension_value_keeps_label_availability_and_reason(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    arguments = {"terms": "Food", "limit": 5, "query": SESSION_STARTS}
    full = adapter.call_tool("discover", {**arguments, "verbosity": "compact"})
    minimal = adapter.call_tool("discover", {**arguments, "verbosity": "minimal"})
    value_id = "dimension.jaffle_item_product_type=jaffle"
    full_value = next(row for row in full["blocked"] if row["id"] == value_id)
    slim_value = next(row for row in minimal["blocked"] if row["id"] == value_id)
    for key in ("id", "kind", "dimension_id", "value", "label", "available", "blocked_reason"):
        assert slim_value[key] == full_value[key], key
    assert slim_value["label"] == "Food"
    assert slim_value["value"] == "jaffle"
    assert slim_value["available"] is False
    assert slim_value["blocked_reason"]
    assert set(slim_value) <= VALUE_KEYS | {"blocked_reason"}


def test_omitted_discover_options_keep_v1_full_cards_and_count(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    default = adapter.call_tool("discover", {"terms": "revenue by store"})
    explicit = adapter.call_tool(
        "discover", {"terms": "revenue by store", "verbosity": "compact", "limit": 10}
    )
    for bucket in ("measures", "metrics", "dimensions", "entities"):
        assert default[bucket] == explicit[bucket]
    assert len(default["measures"]) > 5
    assert {"match_reasons", "starter_query_patch", "topics"} <= set(default["measures"][0])


def test_unavailable_candidates_keep_their_reason(adapter: SemanticLayerMCPAdapter) -> None:
    # With a session measure selected, store objects are out of reach (no join
    # path, or one that needs a grain-aware plan), so discover lists them as blocked.
    response = adapter.call_tool(
        "discover", {"terms": "store", "query": SESSION_STARTS, "verbosity": "minimal"}
    )
    assert response["blocked"]
    for row in response["blocked"]:
        assert row["blocked_reason"], row
        keys = VALUE_KEYS if row["kind"] == "dimension_value" else SLIM_KEYS
        assert set(row) <= keys | {"blocked_reason"}, sorted(set(row) - keys)
    available = [row for bucket in ("measures", "metrics") for row in response[bucket]]
    assert all("blocked_reason" not in row for row in available if row.get("available", True))


def test_full_cards_on_request(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool(
        "discover", {"terms": "revenue by store", "verbosity": "compact", "limit": 10}
    )
    card = response["measures"][0]
    assert {"match_reasons", "starter_query_patch", "topics"} <= set(card)
    assert len(response["measures"]) > 5


@pytest.mark.parametrize(
    ("terms", "bucket", "expected"),
    [
        ("revenue by store", "measures", "measure.jaffle.revenue_usd"),
        ("monthly revenue by store", "measures", "measure.jaffle.revenue_usd"),
        ("customers", "measures", "measure.jaffle.customer_count"),
        ("number of customers", "measures", "measure.jaffle.customer_count"),
        ("orders by store", "measures", "measure.jaffle.order_count"),
        ("average order value", "metrics", "metric.sales.aov_usd"),
        # A qualifier the question does use still wins.
        ("delivered revenue", "measures", "measure.jaffle.delivered_revenue_usd"),
        ("drink revenue share", "metrics", "metric.sales.drink_revenue_share"),
    ],
)
def test_canonical_objects_outrank_near_duplicates(
    adapter: SemanticLayerMCPAdapter, terms: str, bucket: str, expected: str
) -> None:
    response = adapter.call_tool("discover", {"terms": terms})
    assert response[bucket][0]["id"] == expected, [row["id"] for row in response[bucket]]
