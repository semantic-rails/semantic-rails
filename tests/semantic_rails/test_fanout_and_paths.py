from __future__ import annotations

from typing import Any

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
    RelationshipConfig,
    SeedSpec,
    TemporalRoleConfig,
)


def _graph(
    *links: tuple[Any, ...], roots: tuple[str, ...] = (), measure: bool = True, **package: Any
) -> PackageConfig:
    """Entities keyed on ``id`` and linked on it. A link is (source, target, cardinality),
    optionally with relationship fields; ``roots`` limits the entities allowed as a root; with
    ``measure``, entity A has a distinct count and a time role."""
    names = sorted({name for link in links for name in link[:2]})
    return PackageConfig(
        version=1,
        package=PackageMeta(
            package_id="graph",
            name="Graph",
            description="demo",
            default_db=":memory:",
            seed=SeedSpec(kind="sql_script", source="data/seed.sql"),
        ),
        entities=[
            EntityConfig(
                id=name,
                table=name.lower(),
                key_columns=["id"],
                allowed_as_root=not roots or name in roots,
            )
            for name in names
        ],
        dimensions=[
            DimensionConfig(id=f"{name}.id", entity=name, column="id", data_type="id")
            for name in names
        ],
        temporal_roles=[TemporalRoleConfig(id="t.a", dimension="A.id", temporal_class="event_time")]
        if measure
        else [],
        relationships=[
            RelationshipConfig(
                **{
                    "id": f"{source}_{target}",
                    "source_entity": source,
                    "target_entity": target,
                    "source_column": "id",
                    "target_column": "id",
                    "cardinality": cardinality,
                    "safety": "safe",
                    **(extra[0] if extra else {}),
                }
            )
            for source, target, cardinality, *extra in links
        ],
        value_domains=[],
        measures=[
            MeasureConfig(
                id="m.a",
                entity="A",
                row_grain=["A.id"],
                expr="id",
                default_aggregation="count_distinct",
                allowed_aggregations=["count_distinct"],
                compatible_temporal_roles=["t.a"],
            )
        ]
        if measure
        else [],
        metric_recipes=[],
        segments=[],
        **package,
    )


def test_resolve_path_follows_pins_and_reuses_cached_indexes_and_candidates(
    package_config_factory, monkeypatch
):
    config, _ = package_config_factory("jaffle_shop")
    analysis = get_package_analysis(config)
    assert get_package_analysis(config) is analysis
    assert _entity_index(config) is analysis.entities
    assert _relationship_index(config) is analysis.relationships

    path, candidates = resolve_path(
        config, start="entity.jaffle_order", target="entity.jaffle_store"
    )
    assert path == ["relationship.orders_store"]
    assert candidates[0] == ["relationship.orders_store"]

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


def test_resolve_path_handles_cycles_without_recursing_indefinitely():
    config = _graph(("A", "B", "1:1"), ("B", "C", "1:1"), ("C", "A", "1:1"))

    # Both routes around the cycle are found. A's own one-to-one key is the one direct key, so
    # it is used, and the other route comes back with it for the response to disclose.
    assert resolve_path(config, start="A", target="C") == (["C_A"], [["C_A"], ["A_B", "B_C"]])


def test_distinct_values_root_selection_uses_deterministic_tie_breaker():
    # Many-to-one links, so each root reaches D and E by one functional route (one-to-one
    # links would also reach D through E and the other root, a longer one-to-one route).
    config = _graph(
        ("A", "D", "N:1"),
        ("A", "E", "N:1"),
        ("B", "D", "N:1"),
        ("B", "E", "N:1"),
        roots=("A", "B"),
        measure=False,
    )

    compiled = compile_query(config, Registry(config), {"version": 1, "group_by": ["D.id", "E.id"]})

    assert compiled["logical_plan"].root_entity == "A"
    assert compiled["logical_plan"].selected_paths == {"D": ["A_D"], "E": ["A_E"]}


def test_distinct_values_root_selection_rejects_unsafe_fanout():
    config = _graph(("A", "B", "1:N", {"safety": "unsafe"}), roots=("A",), measure=False)

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), {"version": 1, "group_by": ["B.id"]})
    assert exc.value.code == "FANOUT_UNSAFE"
