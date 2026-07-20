from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    PackageConfig,
    PackageMeta,
    SeedSpec,
)


def test_package_config_loads_and_catalog_groups_objects(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    registry = Registry(config)
    grouped = {
        kind: registry.list_objects(kind)
        for kind in [
            "entity",
            "dimension",
            "temporal_role",
            "relationship",
            "value_domain",
            "measure",
            "metric",
        ]
    }

    assert config.package.package_id == "jaffle_shop"
    # Entity count dropped by 2 after daily_metric_rollup/monthly_metric_rollup
    # were migrated to `kind: fact` models (they no longer claim entities).
    assert len(grouped["entity"]) == 13
    assert any(obj.id == "entity.jaffle_order" for obj in grouped["entity"])
    assert any(obj.id == "entity.jaffle_supply" for obj in grouped["entity"])
    assert any(obj.id == "entity.jaffle_time" for obj in grouped["entity"])
    assert any(obj.id == "entity.jaffle_customer_history" for obj in grouped["entity"])
    assert any(obj.id == "entity.jaffle_store_inventory_snapshot" for obj in grouped["entity"])
    assert any(obj.id == "entity.jaffle_order_lifecycle" for obj in grouped["entity"])
    # Fact models do not produce graph entities — measures attach to the
    # `time` entity via `source_relation`.
    assert not any(obj.id == "entity.jaffle_daily_metric_rollup" for obj in grouped["entity"])
    assert not any(obj.id == "entity.jaffle_monthly_metric_rollup" for obj in grouped["entity"])
    assert any(obj.id == "dimension.jaffle_store_name" for obj in grouped["dimension"])
    assert any(obj.id == "temporal_role.jaffle_order_time" for obj in grouped["temporal_role"])
    assert any(obj.id == "measure.jaffle.order_count" for obj in grouped["measure"])
    assert any(obj.id == "metric.sales.aov_usd" for obj in grouped["metric"])


def test_alias_resolution_exact_and_ambiguous(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    registry = Registry(config)

    resolved = registry.resolve("store_name", kind="dimension")
    assert resolved["selected"]["id"] == "dimension.jaffle_store_name"
    assert resolved["selected"]["confidence"] == 0.92
    assert resolved["object"]["id"] == "dimension.jaffle_store_name"

    custom = PackageConfig(
        version=1,
        package=PackageMeta(
            package_id="ambiguous",
            name="Ambiguous Demo",
            description="demo",
            default_db=":memory:",
            seed=SeedSpec(kind="sql_script", source="data/seed.sql"),
        ),
        entities=[
            EntityConfig(
                id="foo.Account", table="foo_account", primary_key="account_id", aliases=["account"]
            ),
            EntityConfig(
                id="bar.Account", table="bar_account", primary_key="account_id", aliases=["account"]
            ),
        ],
        dimensions=[
            DimensionConfig(
                id="foo.Account.account_id",
                entity="foo.Account",
                column="account_id",
                data_type="id",
            ),
            DimensionConfig(
                id="bar.Account.account_id",
                entity="bar.Account",
                column="account_id",
                data_type="id",
            ),
        ],
        temporal_roles=[],
        relationships=[],
        value_domains=[],
        measures=[],
        metric_recipes=[],
        segments=[],
    )
    custom_registry = Registry(custom)

    with pytest.raises(SemanticLayerError) as exc:
        custom_registry.resolve("account", kind="entity")

    assert exc.value.code == "AMBIGUOUS_ALIAS"
    assert len(exc.value.details["candidates"]) == 2
