from __future__ import annotations

from dataclasses import replace

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.compiler_parts.bind import _bind_measure
from semantic_rails.diagnostics import (
    enrich_path_not_found,
    exception_issue,
    recovery_hints_for_error,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import MeasureRefExpr
from semantic_rails.fanout import resolve_path
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
