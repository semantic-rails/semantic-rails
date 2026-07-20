from __future__ import annotations

import pytest

import semantic_rails.fanout as fanout_module
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.indexes import (
    _entity_index,
    _relationship_index,
    get_package_analysis,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import choose_path
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


def test_choose_path_returns_safe_path_for_jaffle_shop(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    path, candidates = choose_path(
        config, start="entity.jaffle_order", target="entity.jaffle_store", hop_limit=4
    )

    assert path == ["relationship.orders_store"]
    assert candidates[0] == ["relationship.orders_store"]


def test_package_analysis_reuses_hot_path_indexes(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    analysis = get_package_analysis(config)

    assert get_package_analysis(config) is analysis
    assert _entity_index(config) is analysis.entities
    assert _relationship_index(config) is analysis.relationships


def test_choose_path_reuses_cached_candidates(package_config_factory, monkeypatch):
    config, _ = package_config_factory("jaffle_shop")
    first_path, _ = fanout_module.choose_path(
        config,
        start="entity.jaffle_order",
        target="entity.jaffle_store",
        hop_limit=4,
    )

    def fail_enumerate_paths(*_args, **_kwargs):
        raise AssertionError("cached choose_path should not enumerate the graph again")

    monkeypatch.setattr(fanout_module, "enumerate_paths", fail_enumerate_paths)
    second_path, _ = fanout_module.choose_path(
        config,
        start="entity.jaffle_order",
        target="entity.jaffle_store",
        hop_limit=4,
    )

    assert second_path == first_path


def test_choose_path_reports_ambiguous_path():
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

    try:
        choose_path(config, start="A", target="D", hop_limit=4)
    except SemanticLayerError as exc:
        assert exc.code == "AMBIGUOUS_PATH"
        assert len(exc.details["candidates"]) == 2


def test_choose_path_honors_allowed_directions():
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

    assert choose_path(config, start="event", target="account", hop_limit=4)[0] == ["event_account"]
    with pytest.raises(SemanticLayerError) as exc:
        choose_path(config, start="account", target="event", hop_limit=4)
    assert exc.value.code == "PATH_NOT_FOUND"


def test_choose_path_handles_cycles_without_recursing_indefinitely():
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

    path, candidates = choose_path(config, start="A", target="C", hop_limit=4)

    assert path in candidates
    assert len(candidates) == 2


def test_distinct_values_root_selection_uses_deterministic_tie_breaker():
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
                cardinality="1:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="A_E",
                source_entity="A",
                target_entity="E",
                source_column="id",
                target_column="id",
                cardinality="1:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="B_D",
                source_entity="B",
                target_entity="D",
                source_column="id",
                target_column="id",
                cardinality="1:1",
                safety="safe",
            ),
            RelationshipConfig(
                id="B_E",
                source_entity="B",
                target_entity="E",
                source_column="id",
                target_column="id",
                cardinality="1:1",
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
