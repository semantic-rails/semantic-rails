from __future__ import annotations

from dataclasses import replace

import pytest

from semantic_rails import fanout as fanout_module
from semantic_rails.ast import normalize_query
from semantic_rails.compiler_parts.bind import _bind_measure
from semantic_rails.compiler_parts.indexes import get_package_analysis
from semantic_rails.diagnostics import (
    enrich_path_not_found,
    exception_issue,
    recovery_hints_for_error,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import MeasureRefExpr
from semantic_rails.fanout import eligible_path_targets, resolve_path
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    PathPolicyConfig,
    PathPreferenceConfig,
    RelationshipConfig,
)


@pytest.mark.parametrize("aggregation", ["count", "percentile", "unknown", None])
def test_unsupported_aggregation_hint_uses_measure_allowlist(package_config_factory, aggregation):
    config, _ = package_config_factory("jaffle_shop")
    measure = replace(
        config.measures[0], allowed_aggregations=["sum", "avg"], default_aggregation="count"
    )
    config = replace(config, measures=[measure])
    query = normalize_query(
        {"select": [{"expression": {"kind": "measure", "measure": measure.id}, "as": "value"}]}
    )
    with pytest.raises(SemanticLayerError) as raised:
        _bind_measure(MeasureRefExpr(measure=measure.id, aggregation=aggregation), config, query)

    issue = exception_issue(raised.value, stage="bind")
    assert issue["code"] == "UNSUPPORTED_AGGREGATION"
    assert issue["details"]["aggregation"] == (aggregation or "count")
    hint = issue["recovery_hints"][0]
    assert hint["kind"] == "use_supported_aggregation"
    assert hint["aggregation_received"] == (aggregation or "count")
    assert hint["allowed"] == issue["details"]["allowed"] == ["sum", "avg"]
    assert hint["message"] == "Choose an allowed aggregation for this measure: sum, avg."


def test_unknown_aggregation_hint_does_not_invent_allowed_values():
    hint = recovery_hints_for_error("UNSUPPORTED_AGGREGATION", {"aggregation": "unknown"})[0]
    assert hint["aggregation_received"] == "unknown"
    assert "count" not in hint["message"]


@pytest.mark.parametrize(
    ("directions", "hop_limit", "diamond", "pinned", "target", "expected"),
    [
        (["forward"], 3, False, False, "D", ["B", "C"]),
        (["reverse"], 3, False, False, "B", []),
        (["forward"], 1, False, False, "C", ["B"]),
        (["forward"], 3, True, False, "E", ["B", "C"]),
        (["forward"], 3, True, True, "E", ["B", "C", "D"]),
    ],
    ids=["forward-only", "reverse-only", "hop-limit", "ambiguous-route", "pinned-route"],
)
@pytest.mark.parametrize("prefilled", [False, True], ids=["raw", "stale-targets"])
def test_path_hint_targets_pass_the_shared_resolver(
    package_config_factory, directions, hop_limit, diamond, pinned, target, expected, prefilled
):
    config, _ = package_config_factory("jaffle_shop")
    edges = (
        [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")] if diamond else [("A", "B"), ("B", "C")]
    )
    config = replace(
        config,
        entities=[EntityConfig(id=node, table=node.lower(), primary_key="id") for node in "ABCDE"],
        dimensions=[
            DimensionConfig(id=f"dimension.{node}", entity=node, column="id", data_type="id")
            for node in "ABCDE"
        ],
        relationships=[
            RelationshipConfig(
                id=f"{source}_{destination}",
                source_entity=source,
                target_entity=destination,
                source_column="id",
                target_column="id",
                cardinality="N:1",
                safety="safe",
                allowed_directions=directions,
            )
            for source, destination in edges
        ],
        path_policy=PathPolicyConfig(max_hops=hop_limit),
        path_preferences=[
            PathPreferenceConfig(
                source_entity="A", target_entity="D", relationship_path=["A_B", "B_D"]
            )
        ]
        if pinned
        else [],
    )
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start="A", target=target)
    if prefilled:
        raised.value.details["reachable_targets"] = ["B", "C", "D", "E"]
        raised.value.details["compatible_group_by_dimensions"] = ["dimension.E"]
    issue = exception_issue(enrich_path_not_found(raised.value, config), stage="plan")
    assert issue["code"] == "PATH_NOT_FOUND"
    assert issue["details"]["reachable_targets"] == expected
    dimensions = [f"dimension.{node}" for node in ["A", *expected]]
    assert issue["details"]["compatible_group_by_dimensions"] == dimensions
    for hint in issue["recovery_hints"]:
        if "reachable_targets" in hint:
            assert hint["reachable_targets"] == expected
        if "compatible_group_by_dimensions" in hint:
            assert hint["compatible_group_by_dimensions"] == dimensions
    for node in expected:
        resolve_path(config, start="A", target=node)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_branching_path_hints_bound_work_and_leave_cache_unchanged(
    package_config_factory, monkeypatch, warm
):
    config, _ = package_config_factory("jaffle_shop")
    layers = [["S"], *[[f"layer_{depth}_{node}" for node in range(4)] for depth in range(8)]]
    nodes = [node for layer in layers for node in layer] + ["disconnected", "Z"]
    config = replace(
        config,
        entities=[EntityConfig(id=node, table=node, primary_key="id") for node in nodes],
        dimensions=[
            DimensionConfig(id=f"dimension.{node}", entity=node, column="id", data_type="id")
            for node in nodes
        ],
        relationships=[
            RelationshipConfig(
                id=f"{source}_{target}",
                source_entity=source,
                target_entity=target,
                source_column="id",
                target_column="id",
                cardinality="1:N",
                safety="safe",
                allowed_directions=["forward"],
            )
            for previous, following in [*zip(layers, layers[1:], strict=False), (["S"], ["Z"])]
            for source in previous
            for target in following
        ],
        path_policy=PathPolicyConfig(max_hops=8),
        path_preferences=[],
    )
    assert len(config.entities) == 35
    assert len(config.relationships) == 117
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start="S", target="disconnected")
    expected = sorted(["Z", *layers[1]])
    issue = exception_issue(enrich_path_not_found(raised.value, config), stage="plan")
    assert issue["details"]["reachable_targets"] == expected
    assert all(hint["kind"] != "isolated_source_entity" for hint in issue["recovery_hints"])
    for target in expected:
        resolve_path(config, start="S", target=target)

    analysis = get_package_analysis(config)
    analysis.path_cache.clear()
    if warm:
        # Preserve both successful routes and existing refusals without rendering them.
        resolve_path(config, start="S", target="Z")
        with pytest.raises(SemanticLayerError):
            resolve_path(config, start="S", target=layers[2][0])
        enrich_path_not_found(raised.value, config)
    cache_before = dict(analysis.path_cache)
    note_cache_before = dict(analysis.route_note_cache)
    shortest_path = fanout_module._shortest_path
    calls = 0

    def counted_shortest_path(*args, **kwargs):
        nonlocal calls
        calls += 1
        return shortest_path(*args, **kwargs)

    def reject_full_resolution(*args, **kwargs):
        pytest.fail("hint eligibility must not enumerate or render full route envelopes")

    monkeypatch.setattr(fanout_module, "_shortest_path", counted_shortest_path)
    monkeypatch.setattr(fanout_module, "resolve_path", reject_full_resolution)
    monkeypatch.setattr(fanout_module, "enumerate_paths", reject_full_resolution)
    monkeypatch.setattr(fanout_module, "_route_decision_required", reject_full_resolution)
    first = enrich_path_not_found(raised.value, config)
    first_calls = calls
    second = enrich_path_not_found(raised.value, config)
    reachable_targets = len(nodes) - 2  # Exclude the source and disconnected entity.
    bound = reachable_targets * (config.path_policy.max_hops + 1) + 1
    assert 0 < first_calls <= bound
    assert 0 < calls - first_calls <= bound
    assert first.details == second.details
    assert first.details["reachable_targets"] == expected
    assert first.details["compatible_group_by_dimensions"] == [
        "dimension.S",
        *[f"dimension.{node}" for node in expected],
    ]
    assert analysis.path_cache == cache_before
    assert all(analysis.path_cache[pair] is cached for pair, cached in cache_before.items())
    assert analysis.route_note_cache == note_cache_before


@pytest.mark.parametrize(
    ("edges", "hop_limit", "expected"),
    [
        ([("A", "B", "1:N")], 2, ["B"]),
        ([("A", "B", "N:1"), ("A", "B", "1:N")], 2, ["B"]),
        ([("A", "B", "N:1"), ("A", "B", "N:1")], 2, []),
        ([("A", "B", "1:N"), ("A", "B", "1:N")], 2, []),
        ([("A", "B", "1:N"), ("B", "C", "N:1"), ("C", "A", "N:1")], 2, ["B", "C"]),
        ([("A", "B", "1:N"), ("B", "C", "N:1")], 1, ["B"]),
    ],
    ids=[
        "sole-nonfunctional",
        "unique-functional",
        "parallel-functional",
        "parallel-nonfunctional",
        "cycle",
        "hop-limit",
    ],
)
def test_lightweight_path_eligibility_agrees_with_full_resolution(
    package_config_factory, edges, hop_limit, expected
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(
        config,
        entities=[EntityConfig(id=node, table=node, primary_key="id") for node in "ABC"],
        relationships=[
            RelationshipConfig(
                id=f"edge_{index}",
                source_entity=source,
                target_entity=target,
                source_column="id",
                target_column="id",
                cardinality=cardinality,
                safety="safe",
                allowed_directions=["forward"],
            )
            for index, (source, target, cardinality) in enumerate(edges)
        ],
        path_policy=PathPolicyConfig(max_hops=hop_limit),
        path_preferences=[],
    )
    assert eligible_path_targets(config, start="A") == expected
    assert not get_package_analysis(config).path_cache
    resolved = []
    for target in "BC":
        try:
            resolve_path(config, start="A", target=target)
        except SemanticLayerError:
            continue
        resolved.append(target)
    assert resolved == expected


def test_path_hints_list_every_single_route_target_in_a_large_tree(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    branches = [f"branch_{index}" for index in range(16)]
    edges = [("S", branch) for branch in branches] + [
        (branch, f"{branch}_leaf_{index}") for branch in branches for index in range(4)
    ]
    targets = sorted(target for _, target in edges)
    # An exhaustive search scans 80 tree edges for each of 80 targets: beyond
    # the former shared budget, even before the alphabetically first failed target.
    assert len(edges) * len(targets) > 4096
    config = replace(
        config,
        entities=[
            EntityConfig(id=node, table=node, primary_key="id")
            for node in ["A_disconnected", "S", *targets]
        ],
        relationships=[
            RelationshipConfig(
                id=f"{source}_{target}",
                source_entity=source,
                target_entity=target,
                source_column="id",
                target_column="id",
                cardinality="1:N",
                safety="safe",
                allowed_directions=["forward"],
            )
            for source, target in edges
        ],
        path_policy=PathPolicyConfig(max_hops=2),
        path_preferences=[],
    )
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start="S", target="A_disconnected")
    analysis = get_package_analysis(config)
    cache_before = dict(analysis.path_cache)
    assert enrich_path_not_found(raised.value, config).details["reachable_targets"] == targets
    assert analysis.path_cache == cache_before
    for target in targets:
        resolve_path(config, start="S", target=target)
