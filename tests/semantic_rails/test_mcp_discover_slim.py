"""MCP discover returns slim cards by default, and canonical objects rank first.

Full cards (match reasons, starter patches, comparison metadata) made
discover over half of a typical agent session's context. verbosity="minimal"
(the default) returns slim cards, which leave out their bucket's kind,
available=true and empty fields; verbosity="compact" returns the full cards.
When the question names an object outright ("revenue by store"), that object
outranks near-duplicates that add a qualifier the question never used
("delivered revenue", "drink revenue").
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions
from semantic_rails.metadata import discover_payload

SLIM_KEYS = {"id", "kind", "label", "measure", "description", "default_temporal_role", "available"}
VALUE_KEYS = {"id", "kind", "dimension_id", "value", "label", "available"}
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
            assert {"id", "label"} <= set(row), (bucket, row)
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
    assert "score" in full_value and "score" not in slim_value


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


@pytest.mark.parametrize("verbosity", ["compact", "full"])
def test_full_cards_on_request(adapter: SemanticLayerMCPAdapter, verbosity: str) -> None:
    response = adapter.call_tool(
        "discover", {"terms": "revenue by store", "verbosity": verbosity, "limit": 10}
    )
    card = response["measures"][0]
    assert {"score", "match_reasons", "starter_query_patch", "topics", "kind"} <= set(card)
    assert len(response["measures"]) > 5
    original = discover_payload(
        adapter.runtime, terms="revenue by store", verbosity=verbosity, limit=10, enforce_scope=True
    )
    for bucket in ("measures", "metrics", "dimensions", "entities", "dimension_values", "blocked"):
        assert response[bucket] == original[bucket]


def test_nonsense_terms_return_a_relevance_block_with_empty_buckets(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    discover = next(tool for tool in list_tool_definitions() if tool["name"] == "discover")
    assert "'out_of_scope' or 'low_relevance' with empty buckets" in discover["description"]
    response = adapter.call_tool("discover", {"terms": "unladen swallow airspeed"})
    assert response.get("out_of_scope") or response.get("low_relevance")
    assert not response["measures"] and not response["metrics"]


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
    "kinds",
    [
        ["measure", "metric"],
        "measure,metric",
        "measure,\nmetric",
        '["measure", "metric"]',
        '[\n  "measure",\n  "metric"\n]',
    ],
)
def test_kinds_filter_accepts_a_list_in_any_encoding(
    adapter: SemanticLayerMCPAdapter, kinds: Any
) -> None:
    response = adapter.call_tool("discover", {"terms": "revenue by store", "kinds": kinds})
    assert not [w for w in response["warnings"] if w["code"] == "DISCOVER_UNKNOWN_KIND"]
    assert response["measures"] and response["metrics"]
    assert not response["dimensions"] and not response["entities"]


@pytest.mark.parametrize(
    "pin", [None, "aggregation", "filter", "window", "temporal_role", "parameters"]
)
def test_only_unpinned_default_aggregates_merge(
    adapter: SemanticLayerMCPAdapter, pin: str | None
) -> None:
    config = adapter.runtime._config
    metric_id, measure_id = "metric.sales.customer_count", "measure.jaffle.customer_count"
    metric = next(m for m in config.metric_recipes if m.id == metric_id)
    metric = replace(metric, temporal_role="", compatible_temporal_roles=[])
    pins = {
        "aggregation": "sum",
        "filter": {"all": []},
        "window": {"grain": "month"},
        "temporal_role": "temporal_role.jaffle_customer_first_order_at",
        "parameters": {"x": 1},
    }
    if pin:
        metric = replace(metric, expression=replace(metric.expression, **{pin: pins[pin]}))
    config.metric_recipes[:] = [metric if m.id == metric_id else m for m in config.metric_recipes]
    if pin is None:
        config.metric_recipes.append(replace(metric, id="metric.sales.customer_count_alias"))
    args = {"terms": "customer count", "limit": 100}
    compact = adapter.call_tool("discover", {**args, "verbosity": "compact"})
    minimal = adapter.call_tool("discover", args)
    assert any(m["id"] == measure_id for m in compact["measures"])
    assert any(m["id"] == metric_id for m in [*compact["metrics"], *compact["blocked"]])
    assert any(m["id"] == measure_id for m in minimal["measures"]) == bool(pin)
    card = next(m for m in [*minimal["metrics"], *minimal["blocked"]] if m["id"] == metric_id)
    assert card.get("measure") == (None if pin else measure_id)
    if pin is None:
        assert sum(m.get("measure") == measure_id for m in minimal["metrics"]) == 1
    measures_only = adapter.call_tool("discover", {**args, "kinds": ["measure"]})
    assert any(m["id"] == measure_id for m in measures_only["measures"])


@pytest.mark.parametrize("reference", [None, "id", "name", "label", "substring"])
def test_description_keeps_whole_sentences_and_object_references(
    adapter: SemanticLayerMCPAdapter, reference: str | None
) -> None:
    config = adapter.runtime._config
    measure = next(m for m in config.measures if m.id == "measure.jaffle.revenue_usd")
    dimension = next(d for d in config.dimensions if d.id == "dimension.jaffle_store_name")
    first = "This introductory sentence provides detailed context for interpretation without prescribing any grouping choices."
    overflow = "Additional explanatory prose that is deliberately long enough to exceed the description allowance."
    last = (
        f"Group by {getattr(dimension, reference)}."
        if reference in ("id", "name", "label")
        else "No further guidance."
    )
    if reference == "substring":
        last = "Superstore namesake."
    description = f"{first} {overflow} {last}"
    config.measures[:] = [
        replace(m, description=description) if m.id == measure.id else m for m in config.measures
    ]
    args = {"terms": "revenue", "kinds": ["measure"], "limit": 100}
    minimal = adapter.call_tool("discover", args)
    card = next(m for m in minimal["measures"] if m["id"] == measure.id)
    assert card["description"] == (
        f"{first} {last}" if reference in ("id", "name", "label") else first
    )
    if reference in ("id", "name", "label"):
        assert len(card["description"]) > 120
    compact = adapter.call_tool("discover", {**args, "verbosity": "compact"})
    assert (
        next(m for m in compact["measures"] if m["id"] == measure.id)["description"] == description
    )
