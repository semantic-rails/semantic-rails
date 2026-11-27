"""MCP discover returns slim cards by default, and canonical objects rank first.

Full cards (match reasons, starter patches, comparison metadata) made
discover over half of a typical agent session's context. verbosity="minimal"
(the default) returns slim cards, which leave out their bucket's kind,
available=true, empty fields and a label whose every word is already in the id;
verbosity="compact" keeps slim cards without blocked metadata; "full" returns full cards.
When the question names an object outright ("revenue by store"), that object
outranks near-duplicates that add a qualifier the question never used
("delivered revenue", "drink revenue").
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.discover_listing import label_restates_id
from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions
from semantic_rails.metadata import _slim_discover_minimal, discover_payload
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

SLIM_KEYS = {"id", "kind", "label", "measure", "description", "default_temporal_role", "available"}
VALUE_KEYS = {"id", "kind", "dimension_id", "value", "label", "available"}
SESSION_STARTS = {"select": [{"as": "s", "expression": "measure.jaffle.session_starts"}]}


@pytest.fixture()
def adapter(jaffle_snapshot) -> Iterator[SemanticLayerMCPAdapter]:
    mcp = SemanticLayerMCPAdapter(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    try:
        yield mcp
    finally:
        mcp.close()


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
            assert "id" in row, (bucket, row)
    assert "terms" not in response and "verbosity" not in response and "stage" not in response
    # An empty bucket is left out.
    assert not {"segments", "dimension_values", "blocked"} & response.keys()


@pytest.mark.parametrize(
    ("terms", "value", "label"),
    [("Food", "jaffle", "Food"), ("Drink", "beverage", "Drink")],
)
def test_minimal_dimension_value_keeps_business_label_and_availability(
    adapter: SemanticLayerMCPAdapter, terms: str, value: str, label: str
) -> None:
    arguments = {"terms": terms, "limit": 5}
    full = adapter.call_tool("discover", {**arguments, "verbosity": "full"})
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
    full = adapter.call_tool("discover", {**arguments, "verbosity": "full"})
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


def test_direct_metadata_minimal_projection_is_unchanged(adapter: SemanticLayerMCPAdapter) -> None:
    args = {"terms": "revenue by store", "limit": 10}
    compact = discover_payload(adapter.runtime, **args, verbosity="compact")
    minimal = discover_payload(adapter.runtime, **args, verbosity="minimal")
    for bucket in ("measures", "metrics", "dimensions", "entities", "dimension_values", "blocked"):
        assert [r["id"] for r in compact[bucket]] == [r["id"] for r in minimal[bucket]]
        for full, slim in zip(compact[bucket], minimal[bucket], strict=True):
            assert slim["score"] == round(full["score"], 1)
            description = " ".join(str(full.get("description") or "").split())
            if description and description != full.get("label"):
                expected = (
                    description[:119].rstrip() + "…" if len(description) > 120 else description
                )
                assert slim["description"] == expected


@pytest.mark.parametrize(
    ("label", "object_id", "restates"),
    [
        ("Store name", "dimension.jaffle_store_name", True),
        ("Item revenue (USD)", "measure.jaffle.item_revenue_usd", True),
        ("Month-over-month revenue growth", "metric.sales.month_over_month_revenue_growth", True),
        ("REVENUE", "measure.jaffle.revenue_usd", True),
        # A word the id lacks is information the card keeps.
        ("Orders", "measure.jaffle.order_count", False),
        ("Rolling 28-day revenue", "metric.sales.rolling_28d_revenue", False),
        ("Food", "dimension.jaffle_item_product_type", False),
        # The kind prefix isn't part of what the id says about the object.
        ("Measure", "measure.jaffle.revenue_usd", False),
        ("", "measure.jaffle.revenue_usd", False),
        ("—", "measure.jaffle.revenue_usd", False),
    ],
)
def test_label_restates_id_only_when_the_id_holds_every_word(
    label: str, object_id: str, restates: bool
) -> None:
    assert label_restates_id(label, object_id) is restates


@pytest.mark.parametrize("verbosity", ["minimal", "compact"])
def test_slim_cards_keep_a_label_only_when_it_adds_a_word_to_the_id(
    adapter: SemanticLayerMCPAdapter, verbosity: str
) -> None:
    args = {"terms": "revenue by store", "limit": 10}
    full = adapter.call_tool("discover", {**args, "verbosity": "full"})
    slim = adapter.call_tool("discover", {**args, "verbosity": verbosity})
    labels = {
        row["id"]: row["label"]
        for bucket in ("measures", "metrics", "dimensions", "entities")
        for row in full[bucket]
    }
    kept = dropped = 0
    for bucket in ("measures", "metrics", "dimensions", "entities"):
        for row in slim[bucket]:
            label = labels.get(row["id"])
            if label is None:  # a metric card standing in for its measure
                continue
            if label_restates_id(label, row["id"]):
                assert "label" not in row, row
                dropped += 1
            else:
                assert row["label"] == label, row
                kept += 1
    assert kept and dropped
    rolling = next(
        row for row in slim["metrics"] if row["id"] == "metric.sales.rolling_28d_revenue"
    )
    assert rolling["label"] == "Rolling 28-day revenue"
    assert all("label" in row for bucket in ("measures", "dimensions") for row in full[bucket])


def test_blocked_cards_keep_their_label(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool("discover", {"terms": "store", "query": SESSION_STARTS})
    assert response["blocked"]
    assert all(row["label"] for row in response["blocked"])


def test_full_cards_on_request(adapter: SemanticLayerMCPAdapter) -> None:
    response = adapter.call_tool(
        "discover", {"terms": "revenue by store", "verbosity": "full", "limit": 10}
    )
    card = response["measures"][0]
    assert {"score", "match_reasons", "starter_query_patch", "topics", "kind"} <= set(card)
    assert len(response["measures"]) > 5
    original = discover_payload(
        adapter.runtime, terms="revenue by store", verbosity="full", limit=10, enforce_scope=True
    )
    for bucket in ("measures", "metrics", "dimensions", "entities", "dimension_values", "blocked"):
        assert response[bucket] == original[bucket]


def test_full_measure_cards_name_the_aggregations_inspect_lists(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    """A median order value is a statistic of revenue: the full card lists every one the measure
    reads, as inspect does, not only the sum its description names; slim cards leave it out."""
    arguments = {"terms": "revenue", "limit": 10}
    full = adapter.call_tool("discover", {**arguments, "verbosity": "full"})
    assert full["measures"]
    for card in full["measures"]:
        inspected = adapter.call_tool("inspect", {"object_id": card["id"]})["card"]
        assert card["allowed_aggregations"] == inspected["allowed_aggregations"], card["id"]
    revenue = next(card for card in full["measures"] if card["id"] == "measure.jaffle.revenue_usd")
    assert {"sum", "median"} <= set(revenue["allowed_aggregations"])
    for verbosity in ("minimal", "compact"):
        slim = adapter.call_tool("discover", {**arguments, "verbosity": verbosity})
        assert not any("allowed_aggregations" in card for card in slim["measures"]), verbosity


@pytest.mark.parametrize("terms", ["revenue by store", "store"])
def test_compact_discover_keeps_slim_descriptions_without_blocked_metadata(
    adapter: SemanticLayerMCPAdapter, terms: str
) -> None:
    arguments = {"terms": terms, "query": SESSION_STARTS, "limit": 5}
    minimal = adapter.call_tool("discover", {**arguments, "verbosity": "minimal"})
    compact = adapter.call_tool("discover", {**arguments, "verbosity": "compact"})
    assert compact["ok"]
    assert "blocked" not in compact
    for bucket in ("measures", "metrics", "dimensions", "entities", "dimension_values"):
        assert compact.get(bucket, []) == [
            row for row in minimal.get(bucket, []) if row.get("available") is not False
        ]
        for card in compact.get(bucket, []):
            assert (
                not {
                    "match_reasons",
                    "recommended_next_actions",
                    "companions",
                    "starter_query_patch",
                    "blocked_reason",
                    "recommended_dimensions",
                }
                & card.keys()
            )


def test_nonsense_terms_return_a_relevance_block_with_empty_buckets(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    discover = next(tool for tool in list_tool_definitions() if tool["name"] == "discover")
    assert "'out_of_scope' or 'low_relevance' with no candidates" in discover["description"]
    response = adapter.call_tool("discover", {"terms": "unladen swallow airspeed"})
    assert response.get("out_of_scope") or response.get("low_relevance")
    assert not {"measures", "metrics", "dimensions"} & response.keys()


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
    assert not [w for w in response.get("warnings", []) if w["code"] == "DISCOVER_UNKNOWN_KIND"]
    assert response["measures"] and response["metrics"]
    assert "dimensions" not in response and "entities" not in response


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
    args = {"terms": "customer count", "limit": 10, "kinds": ["measure", "metric"]}
    full = adapter.call_tool("discover", {**args, "verbosity": "full"})
    minimal = adapter.call_tool("discover", args)
    assert any(m["id"] == measure_id for m in full["measures"])
    assert any(m["id"] == metric_id for m in [*full["metrics"], *full["blocked"]])
    assert any(m["id"] == measure_id for m in minimal.get("measures", [])) == bool(pin)
    card = next(
        m for m in [*minimal["metrics"], *minimal.get("blocked", [])] if m["id"] == metric_id
    )
    assert card.get("measure") == (None if pin else measure_id)
    if pin is None:
        assert sum(m.get("measure") == measure_id for m in minimal["metrics"]) == 1
        assert {metric_id, "metric.sales.customer_count_alias"} <= {
            m["id"] for m in minimal["metrics"]
        }
    measures_only = adapter.call_tool("discover", {**args, "kinds": ["measure"]})
    assert any(m["id"] == measure_id for m in measures_only["measures"])


@pytest.mark.parametrize("blocked", ["metric", "measure"])
def test_unavailable_aggregate_pair_keeps_both_cards(
    adapter: SemanticLayerMCPAdapter, blocked: str
) -> None:
    config = adapter.runtime._config
    metric = next(m for m in config.metric_recipes if m.id == "metric.sales.customer_count")
    metric = replace(metric, temporal_role="", compatible_temporal_roles=[])
    config.metric_recipes[:] = [metric]
    measure_id = metric.expression.measure
    payload = {
        "metrics": [{"id": metric.id, "available": blocked != "metric"}],
        "measures": [{"id": measure_id, "available": blocked != "measure"}],
    }
    result = _slim_discover_minimal(payload, config)
    assert result["metrics"] == [{"id": metric.id, "available": blocked != "metric"}]
    assert result["measures"] == [{"id": measure_id, "available": blocked != "measure"}]


@pytest.mark.parametrize("target", ["metric", "measure"])
@pytest.mark.parametrize("action", ["allow", "deny"])
def test_policy_targeted_pair_keeps_both_cards(
    adapter: SemanticLayerMCPAdapter, target: str, action: str
) -> None:
    config = adapter.runtime._config
    metric = next(m for m in config.metric_recipes if m.id == "metric.sales.customer_count")
    metric = replace(metric, temporal_role="", compatible_temporal_roles=[])
    config.metric_recipes[:] = [metric]
    measure_id = metric.expression.measure
    config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.customer_access",
            kind="object_access",
            object_ids=[metric.id if target == "metric" else measure_id],
            action=action,
        )
    )
    payload = {"metrics": [{"id": metric.id}], "measures": [{"id": measure_id}]}
    result = _slim_discover_minimal(payload, config)
    assert result["metrics"] == [{"id": metric.id}]
    assert result["measures"] == [{"id": measure_id}]


def test_denied_metric_does_not_hide_allowed_equivalent_or_measure(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    config = adapter.runtime._config
    metric = next(m for m in config.metric_recipes if m.id == "metric.sales.customer_count")
    metric = replace(metric, temporal_role="", compatible_temporal_roles=[])
    alias = replace(metric, id="metric.sales.customer_count_alias")
    config.metric_recipes[:] = [metric, alias]
    config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.customer_access", kind="object_access", object_ids=[metric.id], action="deny"
        )
    )
    runtime = Runtime.from_config(config, source_path=adapter.runtime.source_path)
    mcp = SemanticLayerMCPAdapter(runtime)
    try:
        args = {"terms": "customer count", "limit": 10, "kinds": ["measure", "metric"]}
        full = mcp.call_tool("discover", {**args, "verbosity": "full"})
        minimal = mcp.call_tool("discover", args)
        measure_id = metric.expression.measure
        assert {metric.id, alias.id} <= {row["id"] for row in minimal["metrics"]}
        assert any(row["id"] == measure_id for row in full["measures"])
        assert any(row.get("measure") == measure_id for row in minimal["metrics"])
        for expression, ok in ((metric.id, False), (alias.id, True), (measure_id, True)):
            query = {"select": [{"as": "value", "expression": expression}]}
            result = runtime.validate(query)
            assert result["ok"] is ok, result
            if not ok:
                assert result["errors"][0]["code"] == "POLICY_DENIED"
    finally:
        mcp.close()


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
    full = adapter.call_tool("discover", {**args, "verbosity": "full"})
    assert next(m for m in full["measures"] if m["id"] == measure.id)["description"] == description
