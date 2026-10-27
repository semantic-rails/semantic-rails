from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata import catalog_payload, discover_payload, inspect_payload
from semantic_rails.schema import SemanticPolicyConfig


@pytest.fixture()
def runtime(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


def test_package_loader_and_metadata_surface_segments(runtime):
    catalog = catalog_payload(runtime)
    discovered = discover_payload(runtime, terms="high value customers", kinds=["segment"], limit=5)
    inspected = inspect_payload(runtime, object_id="segment.jaffle.high_value_customers")["card"]

    assert any(row["id"] == "segment.jaffle.high_value_customers" for row in catalog["segments"])
    assert discovered["segments"]
    assert discovered["segments"][0]["id"] == "segment.jaffle.high_value_customers"
    assert inspected["kind"] == "segment"
    assert inspected["basis_metric"] == "metric.sales.customer_count"
    assert "dimension.jaffle_customer_id" in inspected["member_key_dimensions"]
    assert "dimension.jaffle_customer_name" in inspected["preview_dimensions"]
    assert (
        inspected["starter_preview_request"]["segment_id"] == "segment.jaffle.high_value_customers"
    )


def test_segment_operations_enforce_policy_context_before_metadata_or_rows(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    config = runtime.config
    config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.test.hide_customer_segment",
            kind="object_visibility",
            object_ids=["segment.jaffle.high_value_customers"],
            audiences=["external"],
            action="hidden",
        )
    )
    old_runtime = runtime
    runtime = type(old_runtime).from_config(
        config,
        source_path=old_runtime.source_path,
        package_id=old_runtime.package_id,
        prefer_package_root_assets=old_runtime.prefer_package_root_assets,
    )
    old_runtime.close()
    context = {"audience": "external", "tenant": "tenant-a"}
    try:
        validated = runtime.segment_validate(
            "segment.jaffle.high_value_customers", policy_context=context
        )
        assert validated["ok"] is False
        # A hidden segment is refused exactly like one that does not exist.
        assert validated["errors"][0]["code"] == "OBJECT_NOT_FOUND"
        assert "normalized_segment" not in validated
        assert validated["request_context"]["audience"] == "external"

        unknown = "Unknown segment 'segment.jaffle.high_value_customers'"
        with pytest.raises(SemanticLayerError, match=unknown):
            runtime.segment_explain("segment.jaffle.high_value_customers", policy_context=context)
        with pytest.raises(SemanticLayerError, match=unknown):
            runtime.segment_preview("segment.jaffle.high_value_customers", policy_context=context)
    finally:
        runtime.close()
