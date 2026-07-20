from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata import catalog_payload, discover_payload, inspect_payload
from semantic_rails.schema import SemanticPolicyConfig
from semantic_rails.segments import normalize_segment


def test_package_loader_and_metadata_surface_segments(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        catalog = catalog_payload(runtime)
        discovered = discover_payload(
            runtime, terms="high value customers", kinds=["segment"], limit=5
        )
        inspected = inspect_payload(runtime, object_id="segment.jaffle.high_value_customers")[
            "card"
        ]

        assert any(
            row["id"] == "segment.jaffle.high_value_customers" for row in catalog["segments"]
        )
        assert discovered["segments"]
        assert discovered["segments"][0]["id"] == "segment.jaffle.high_value_customers"
        assert inspected["kind"] == "segment"
        assert inspected["basis_metric"] == "metric.sales.customer_count"
        assert "dimension.jaffle_customer_id" in inspected["member_key_dimensions"]
        assert "dimension.jaffle_customer_name" in inspected["preview_dimensions"]
        assert (
            inspected["starter_preview_request"]["segment_id"]
            == "segment.jaffle.high_value_customers"
        )
    finally:
        runtime.close()


def test_runtime_segment_validate_and_preview(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        validated = runtime.segment_validate("segment.jaffle.high_value_customers")
        preview = runtime.segment_preview("segment.jaffle.high_value_customers", limit=5)
        explained = runtime.segment_explain("segment.jaffle.high_value_customers")

        assert validated["ok"] is True
        assert validated["normalized_segment"]["entity"] == "entity.jaffle_customer"
        assert validated["derived_query"]["group_by"][0] == "dimension.jaffle_customer_id"
        assert preview["member_count"] >= preview["preview_row_count"] >= 1
        assert preview["rows"]
        assert "__segment_basis" not in preview["rows"][0]
        assert "dimension.jaffle_customer_id" in preview["rows"][0]
        assert explained["segment"]["id"] == "segment.jaffle.high_value_customers"
        assert explained["rendered_sql"]
    finally:
        runtime.close()


def test_segment_operations_enforce_policy_context_before_metadata_or_rows(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    runtime.config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.test.hide_customer_segment",
            kind="object_visibility",
            object_ids=["segment.jaffle.high_value_customers"],
            audiences=["external"],
            action="hidden",
        )
    )
    context = {"audience": "external", "tenant": "tenant-a"}
    try:
        validated = runtime.segment_validate(
            "segment.jaffle.high_value_customers", policy_context=context
        )
        assert validated["ok"] is False
        assert validated["errors"][0]["code"] == "POLICY_DENIED"
        assert "normalized_segment" not in validated
        assert validated["request_context"]["audience"] == "external"

        with pytest.raises(SemanticLayerError, match="blocked by policy"):
            runtime.segment_explain("segment.jaffle.high_value_customers", policy_context=context)
        with pytest.raises(SemanticLayerError, match="blocked by policy"):
            runtime.segment_preview("segment.jaffle.high_value_customers", policy_context=context)
    finally:
        runtime.close()


def test_normalize_segment_rejects_preview_dimensions_on_other_entities(package_config_factory):
    _, config_path = package_config_factory("jaffle_shop")
    segment_file = Path(config_path) / "segments" / "invalid.yml"
    segment_file.parent.mkdir(parents=True, exist_ok=True)
    segment_file.write_text(
        yaml.safe_dump(
            {
                "segment": {
                    "id": "segment.jaffle.invalid_customer_segment",
                    "name": "jaffle.Customer.invalid_customer_segment",
                    "label": "Invalid customer segment",
                    "entity": "entity.jaffle_customer",
                    "basis_metric": "metric.sales.customer_count",
                    "preview_dimensions": ["dimension.jaffle_store_name"],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_package_config(str(config_path))
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_segment(config, "segment.jaffle.invalid_customer_segment")

    assert excinfo.value.code == "INVALID_SEGMENT"
