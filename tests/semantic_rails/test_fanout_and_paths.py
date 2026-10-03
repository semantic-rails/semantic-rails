from __future__ import annotations

from dataclasses import replace

import pytest

import semantic_rails.fanout as fanout_module
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.indexes import (
    _entity_index,
    _relationship_index,
    get_package_analysis,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import resolve_path
from semantic_rails.registry import Registry
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    PackageConfig,
    PackageMeta,
    PathPreferenceConfig,
    RelationshipConfig,
    SeedSpec,
    TemporalRoleConfig,
)


def test_resolve_path_returns_the_pinned_path_for_jaffle_shop(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    path, candidates = resolve_path(
        config, start="entity.jaffle_order", target="entity.jaffle_store"
    )

    assert path == ["relationship.orders_store"]
    assert candidates[0] == ["relationship.orders_store"]


def test_package_analysis_reuses_hot_path_indexes(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    analysis = get_package_analysis(config)

    assert get_package_analysis(config) is analysis
    assert _entity_index(config) is analysis.entities
    assert _relationship_index(config) is analysis.relationships


def test_resolve_path_reuses_cached_candidates(package_config_factory, monkeypatch):
    config, _ = package_config_factory("jaffle_shop")
    # An unpinned pair, so the answer comes from enumeration and then the cache.
    first_path, _ = fanout_module.resolve_path(
        config, start="entity.jaffle_item", target="entity.jaffle_product"
    )

    def fail_enumerate_paths(*_args, **_kwargs):
        raise AssertionError("cached resolve_path should not enumerate the graph again")

    monkeypatch.setattr(fanout_module, "enumerate_paths", fail_enumerate_paths)
    second_path, _ = fanout_module.resolve_path(
        config, start="entity.jaffle_item", target="entity.jaffle_product"
    )

    assert second_path == first_path


def test_resolve_path_refuses_a_tie_and_follows_its_pin():
    config = PackageConfig(
        version=1,
        package=PackageMeta(
            package_id="ambiguous",
            name="Ambiguous",
            description="demo",
            default_db=":memory:",
            seed=SeedSpec(kind="sql_script", source="data/seed.sql"),
        ),
        entities=[
            EntityConfig(id="A", table="a", primary_key="id"),
            EntityConfig(id="B", table="b", primary_key="id"),
            EntityConfig(id="C", table="c", primary_key="id"),
            EntityConfig(id="D", table="d", primary_key="id"),
        ],
        dimensions=[
            DimensionConfig(id="A.id", entity="A", column="id", data_type="id"),
            DimensionConfig(id="B.id", entity="B", column="id", data_type="id"),
            DimensionConfig(id="C.id", entity="C", column="id", data_type="id"),
            DimensionConfig(id="D.id", entity="D", column="id", data_type="id"),
        ],
        temporal_roles=[
            TemporalRoleConfig(id="t.a", dimension="A.id", temporal_class="event_time")
        ],
        relationships=[
            RelationshipConfig(
                id="A_B",
                source_entity="A",
                target_entity="B",
                source_column="id",
                target_column="id",
                cardinality="1:N",
                safety="safe",
            ),
            RelationshipConfig(
                id="A_C",
                source_entity="A",
                target_entity="C",
                source_column="id",
                target_column="id",
                cardinality="1:N",
                safety="safe",
            ),
            RelationshipConfig(
                id="B_D",
                source_entity="B",
                target_entity="D",
                source_column="id",
                target_column="id",
                cardinality="1:N",
                safety="safe",
            ),
            RelationshipConfig(
                id="C_D",
                source_entity="C",
                target_entity="D",
                source_column="id",
                target_column="id",
                cardinality="1:N",
                safety="safe",
            ),
        ],
        value_domains=[],
        measures=[
            MeasureConfig(
                id="m.a",
                entity="A",
                subject_entity="A",
                aggregation_entity="A",
                row_grain=["A.id"],
                expr="id",
                default_aggregation="count_distinct",
                allowed_aggregations=["count_distinct"],
                compatible_temporal_roles=["t.a"],
            )
        ],
        metric_recipes=[],
        segments=[],
        path_preferences=[
            PathPreferenceConfig(
                source_entity="A", target_entity="D", relationship_path=["A_B", "B_D"]
            )
        ],
    )

    assert resolve_path(config, start="A", target="D") == (["A_B", "B_D"], [["A_B", "B_D"]])
    with pytest.raises(SemanticLayerError) as exc:
        resolve_path(replace(config, path_preferences=[]), start="A", target="D")
    assert exc.value.code == "AMBIGUOUS_PATH"
    options = exc.value.details["clarification"]["options"]
    assert [option["relationship_path"] for option in options] == [["A_B", "B_D"], ["A_C", "C_D"]]
    assert options[1]["decision"] == {
        "source_entity": "A",
        "target_entity": "D",
        "relationship_path": ["A_C", "C_D"],
        "label": options[1]["meaning"],
    }


def test_resolve_path_honors_allowed_directions():
    config = PackageConfig(
        version=1,
        package=PackageMeta(
            package_id="directed",
            name="Directed",
            description="demo",
            default_db=":memory:",
            seed=SeedSpec(kind="sql_script", source="data/seed.sql"),
        ),
        entities=[
            EntityConfig(id="event", table="event", primary_key="event_id"),
            EntityConfig(id="account", table="account", primary_key="account_id"),
        ],
        dimensions=[
            DimensionConfig(id="event.event_id", entity="event", column="event_id", data_type="id"),
            DimensionConfig(
                id="account.account_id", entity="account", column="account_id", data_type="id"
            ),
        ],
        temporal_roles=[
            TemporalRoleConfig(
                id="t.event", dimension="event.event_id", temporal_class="event_time"
            )
        ],
        relationships=[
            RelationshipConfig(
                id="event_account",
                source_entity="event",
                target_entity="account",
                source_column="account_id",
                target_column="account_id",
                cardinality="N:1",
                safety="safe",
                allowed_directions=["forward"],
            ),
        ],
        value_domains=[],
        measures=[
            MeasureConfig(
                id="m.event",
                entity="event",
                subject_entity="event",
                aggregation_entity="event",
                row_grain=["event.event_id"],
                expr="event_id",
                default_aggregation="count_distinct",
                allowed_aggregations=["count_distinct"],
                compatible_temporal_roles=["t.event"],
            )
        ],
        metric_recipes=[],
        segments=[],
    )

    assert resolve_path(config, start="event", target="account")[0] == ["event_account"]
    with pytest.raises(SemanticLayerError) as exc:
        resolve_path(config, start="account", target="event")
    assert exc.value.code == "PATH_NOT_FOUND"


def test_resolve_path_handles_cycles_without_recursing_indefinitely():
    config = PackageConfig(
        version=1,
        package=PackageMeta(
            package_id="cycle",
            name="Cycle",
            description="demo",
            default_db=":memory:",
            seed=SeedSpec(kind="sql_script", source="data/seed.sql"),
        ),
        entities=[
            EntityConfig(id="A", table="a", primary_key="id"),
            EntityConfig(id="B", table="b", primary_key="id"),
            EntityConfig(id="C", table="c", primary_key="id"),
        ],
        dimensions=[
            DimensionConfig(id="A.id", entity="A", column="id", data_type="id"),
            DimensionConfig(id="B.id", entity="B", column="id", data_type="id"),
            DimensionConfig(id="C.id", entity="C", column="id", data_type="id"),
        ],
        temporal_roles=[
            TemporalRoleConfig(id="t.a", dimension="A.id", temporal_class="event_time")
        ],
        relationships=[
            RelationshipConfig(
                id="A_B",
                source_entity="A",
                target_entity="B",
                source_column="id",
                target_column="id",
                cardinality="1:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="B_C",
                source_entity="B",
                target_entity="C",
                source_column="id",
                target_column="id",
                cardinality="1:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="C_A",
                source_entity="C",
                target_entity="A",
                source_column="id",
                target_column="id",
                cardinality="1:1",
                safety="safe",
            ),
        ],
        value_domains=[],
        measures=[
            MeasureConfig(
                id="m.a",
                entity="A",
                subject_entity="A",
                aggregation_entity="A",
                row_grain=["A.id"],
                expr="id",
                default_aggregation="count_distinct",
                allowed_aggregations=["count_distinct"],
                compatible_temporal_roles=["t.a"],
            )
        ],
        metric_recipes=[],
        segments=[],
    )

    # Both routes around the cycle are found. A's own one-to-one key is the one direct key, so
    # it is used, and the other route comes back with it for the response to disclose.
    assert resolve_path(config, start="A", target="C") == (["C_A"], [["C_A"], ["A_B", "B_C"]])


def test_distinct_values_root_selection_uses_deterministic_tie_breaker():
    # Many-to-one links, so each root reaches D and E by one functional route (one-to-one
    # links would also reach D through E and the other root, a longer one-to-one route).
    config = PackageConfig(
        version=1,
        package=PackageMeta(
            package_id="ambiguous_root",
            name="Ambiguous root",
            description="demo",
            default_db=":memory:",
            seed=SeedSpec(kind="sql_script", source="data/seed.sql"),
        ),
        entities=[
            EntityConfig(id="A", table="a", primary_key="id", allowed_as_root=True),
            EntityConfig(id="B", table="b", primary_key="id", allowed_as_root=True),
            EntityConfig(id="D", table="d", primary_key="id", allowed_as_root=False),
            EntityConfig(id="E", table="e", primary_key="id", allowed_as_root=False),
        ],
        dimensions=[
            DimensionConfig(id="A.id", entity="A", column="id", data_type="id"),
            DimensionConfig(id="B.id", entity="B", column="id", data_type="id"),
            DimensionConfig(id="D.id", entity="D", column="id", data_type="id"),
            DimensionConfig(id="E.id", entity="E", column="id", data_type="id"),
        ],
        temporal_roles=[],
        relationships=[
            RelationshipConfig(
                id="A_D",
                source_entity="A",
                target_entity="D",
                source_column="id",
                target_column="id",
                cardinality="N:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="A_E",
                source_entity="A",
                target_entity="E",
                source_column="id",
                target_column="id",
                cardinality="N:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="B_D",
                source_entity="B",
                target_entity="D",
                source_column="id",
                target_column="id",
                cardinality="N:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="B_E",
                source_entity="B",
                target_entity="E",
                source_column="id",
                target_column="id",
                cardinality="N:1",
                safety="safe",
            ),
        ],
        value_domains=[],
        measures=[],
        metric_recipes=[],
        segments=[],
    )

    compiled = compile_query(config, Registry(config), {"version": 1, "group_by": ["D.id", "E.id"]})

    assert compiled["logical_plan"].root_entity == "A"
    assert compiled["logical_plan"].selected_paths == {"D": ["A_D"], "E": ["A_E"]}


def test_distinct_values_root_selection_rejects_unsafe_fanout():
    config = PackageConfig(
        version=1,
        package=PackageMeta(
            package_id="unsafe_root",
            name="Unsafe root",
            description="demo",
            default_db=":memory:",
            seed=SeedSpec(kind="sql_script", source="data/seed.sql"),
        ),
        entities=[
            EntityConfig(id="A", table="a", primary_key="id", allowed_as_root=True),
            EntityConfig(id="B", table="b", primary_key="id", allowed_as_root=False),
        ],
        dimensions=[
            DimensionConfig(id="A.id", entity="A", column="id", data_type="id"),
            DimensionConfig(id="B.id", entity="B", column="id", data_type="id"),
        ],
        temporal_roles=[],
        relationships=[
            RelationshipConfig(
                id="A_B",
                source_entity="A",
                target_entity="B",
                source_column="id",
                target_column="id",
                cardinality="1:N",
                safety="unsafe",
            ),
        ],
        value_domains=[],
        measures=[],
        metric_recipes=[],
        segments=[],
    )

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), {"version": 1, "group_by": ["B.id"]})
    assert exc.value.code == "FANOUT_UNSAFE"
