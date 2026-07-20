"""Inspect starter patches — blind-agent UX teaches `where`, `order_by`,
and `metric_filters` shapes by example, not by trial-and-error against
``validate``.

The original patch set was `select`/`group_by`/`time` only, which forced
blind agents to guess the other clause shapes. These tests pin the
extended contract: every patch is runnable and the new patches reference
their target object.
"""

from __future__ import annotations

from semantic_rails.metadata import inspect_payload


def _patches_by_kind(card: dict) -> dict[str, dict]:
    return {str(p["kind"]): p for p in list(card.get("starter_query_patches", []) or [])}


def test_inspect_measure_ships_order_by_and_where_patches(runtime_factory) -> None:
    """For a measure whose recommended_dimensions are mostly ID-typed
    (revenue_usd recommends order_store_id, order_customer_id, etc.),
    the where-patch search now falls through to broader config
    dimensions reachable from the same entity so the scaffold still
    fires. Without this, the most common authoring pattern (ID-typed
    relations) silently dropped the where patch."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = inspect_payload(runtime, object_id="measure.jaffle.revenue_usd")
        card = payload["card"]
        patches = _patches_by_kind(card)
        # Existing patches still present.
        assert "select" in patches, "select patch missing"
        assert "time" in patches, "time patch missing"
        # New patches.
        assert "order_by" in patches, f"order_by patch missing; got {list(patches)}"
        order_patch = patches["order_by"]["query_patch"]
        assert order_patch.get("order_by"), "order_by patch has no order_by clause"
        assert order_patch["order_by"][0]["field"] == "time"
        # `where` patch should now fire for revenue_usd because the
        # broadened candidate search finds a non-ID dimension with a
        # declared value_domain.
        assert "where" in patches, (
            f"where patch missing — broadened candidate search should find a "
            f"non-ID dimension with a value_domain; got patches={list(patches)}"
        )
        where_patch = patches["where"]["query_patch"]
        assert where_patch.get("where"), "where patch has no where clause"
        first = where_patch["where"][0]
        assert "field" in first and "value" in first and "op" in first
        # The scaffold must not name an ID-typed dimension.
        from semantic_rails.metadata_parts.path_coverage import _value_domain_for_dimension

        dim_id = first["field"]
        domain = _value_domain_for_dimension(runtime.config, dim_id)
        assert domain is not None, f"where patch references {dim_id} without value_domain"
    finally:
        runtime.close()


def test_inspect_predicate_metric_ships_metric_filters_patch(runtime_factory) -> None:
    """Predicate-backed metrics — only those expose a `metric_filters`
    starter patch. Use the registry to find a predicate-backed metric in
    jaffle_shop. If none exist, the test is a no-op (presence-when-meaningful
    contract, not absence-otherwise)."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # Find any predicate-backed metric by scanning the registry.
        predicate_metric_id = ""
        for obj in runtime.registry.list_objects(kind="metric"):
            card = inspect_payload(runtime, object_id=obj.id)["card"]
            if int(card.get("predicate_count", 0) or 0) > 0:
                predicate_metric_id = obj.id
                break

        if not predicate_metric_id:
            return  # no predicate metrics in this package — nothing to assert

        card = inspect_payload(runtime, object_id=predicate_metric_id)["card"]
        patches = _patches_by_kind(card)
        assert "metric_filters" in patches, (
            f"predicate metric {predicate_metric_id} missing metric_filters patch; "
            f"got {list(patches)}"
        )
        mf_patch = patches["metric_filters"]["query_patch"]
        first = list(mf_patch.get("metric_filters", []) or [])[0]
        expr = dict(first.get("expression", {}) or {})
        # The scaffold uses the simpler MetricRefExpr shape — the full
        # metric_predicate union accepts that without requiring `entity`
        # or `input` fields the agent would have to fill in.
        assert expr.get("kind") == "metric"
        assert expr.get("metric") == predicate_metric_id
        assert first.get("op") and "value" in first
    finally:
        runtime.close()


def test_inspect_starter_patches_all_validate(runtime_factory) -> None:
    """Every starter patch shipped on a measure inspect card must be a
    runnable Query IR (no validation errors)."""
    runtime = runtime_factory("jaffle_shop")
    try:
        card = inspect_payload(runtime, object_id="measure.jaffle.revenue_usd")["card"]
        for entry in list(card.get("starter_query_patches", []) or []):
            patch = entry["query_patch"]
            result = runtime.validate(patch)
            assert not result.get("errors"), (
                f"starter patch kind={entry['kind']} produced validation errors: {result['errors']}"
            )
    finally:
        runtime.close()
