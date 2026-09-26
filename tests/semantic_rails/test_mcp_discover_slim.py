"""MCP discover returns slim cards by default, and canonical objects rank first.

Full cards (match reasons, starter patches, comparison metadata) made
discover over half of a typical agent session's context. verbosity="minimal"
(the default) returns slim cards; verbosity="compact" adds root entities, at
most three match reasons and starter patches; verbosity="full" returns the
whole cards. Below "full", a card leaves out its bucket's kind, available=true
and empty fields. When the question names an object outright ("revenue by
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


def test_discover_advertises_slim_default_and_full_card_opt_in() -> None:
    discover = next(tool for tool in list_tool_definitions() if tool["name"] == "discover")
    properties = discover["inputSchema"]["properties"]
    assert properties["verbosity"]["default"] == "minimal"
    assert properties["verbosity"]["enum"] == ["minimal", "compact", "full"]
    assert "default" not in properties["limit"]  # empty terms page at 100 ids by default


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
            assert set(row) <= SLIM_KEYS - {"kind", "available"}, (bucket, sorted(set(row)))
            assert {"id", "label", "score"} <= set(row), (bucket, row)
    assert "terms" not in response and "verbosity" not in response
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


def test_compact_and_full_cards_on_request(adapter: SemanticLayerMCPAdapter) -> None:
    arguments = {"terms": "revenue by store", "limit": 10}
    compact = adapter.call_tool("discover", {**arguments, "verbosity": "compact"})
    full = adapter.call_tool("discover", {**arguments, "verbosity": "full"})
    card, whole = compact["measures"][0], full["measures"][0]
    assert card["id"] == whole["id"]
    assert {"match_reasons", "starter_query_patch", "root_entity", "description"} <= set(card)
    assert card["starter_query_patch"] == whole["starter_query_patch"]
    assert card["match_reasons"] == whole["match_reasons"][:3]
    # Repeats of the id, kind or label, builder metadata and empty fields stay in "full".
    assert not {"kind", "name", "object_type", "topics", "available"} & set(card)
    assert {"kind", "name", "object_type", "topics", "available"} <= set(whole)
    assert len(compact["measures"]) == len(full["measures"]) > 5


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


@pytest.mark.parametrize(
    "kinds", [["measure", "metric"], "measure,metric", '["measure", "metric"]']
)
def test_kinds_filter_accepts_a_list_in_any_encoding(
    adapter: SemanticLayerMCPAdapter, kinds: Any
) -> None:
    response = adapter.call_tool("discover", {"terms": "revenue by store", "kinds": kinds})
    assert not [w for w in response["warnings"] if w["code"] == "DISCOVER_UNKNOWN_KIND"]
    assert response["measures"] and response["metrics"]
    assert not response["dimensions"] and not response["entities"]
