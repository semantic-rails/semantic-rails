"""The semantic DAG of a query: which measures, metrics and expressions each output reads."""

from __future__ import annotations

from typing import Any

from ..ast import (
    NormalizedQuery,
    time_output_alias,
)
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    ComparisonExpr,
    DistributionExpr,
    EntityValueExpr,
    MeasureRefExpr,
    MetricRecipeRefExpr,
    RatioExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    expr_kind,
    expr_to_dict,
)
from ..ir import (
    SemanticDag,
    SemanticDagNode,
)
from ..schema import (
    PackageConfig,
)
from .indexes import (
    _measure_index,
    _recipe_index,
)


def _semantic_dag_for_query(query: NormalizedQuery, config: PackageConfig) -> SemanticDag:
    nodes: list[SemanticDagNode] = []
    outputs: dict[str, str] = {}
    counter = 0

    def _next_id(kind: str) -> str:
        nonlocal counter
        counter += 1
        return f"{kind}_{counter}"

    def _walk(expr: SemanticExpr, *, label: str = "") -> str:
        if isinstance(expr, MeasureRefExpr):
            node_id = _next_id("source_leaf")
            measure = _measure_index(config).get(expr.measure)
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="SourceLeaf",
                    label=label or expr.measure,
                    details={
                        "measure": expr.measure,
                        "aggregation": expr.aggregation
                        or (measure.default_aggregation if measure else ""),
                        "entity": measure.entity if measure else "",
                        "temporal_roles": list(
                            measure.compatible_temporal_roles if measure else []
                        ),
                    },
                )
            )
            return node_id
        if isinstance(expr, AggregateExpr):
            node_id = _next_id("source_leaf")
            measure = _measure_index(config).get(expr.measure)
            details = {
                "measure": expr.measure,
                "aggregation": expr.aggregation or (measure.default_aggregation if measure else ""),
                "entity": measure.entity if measure else "",
                "temporal_roles": list(measure.compatible_temporal_roles if measure else []),
                "filter": dict(expr.filter),
            }
            nodes.append(
                SemanticDagNode(
                    id=node_id, kind="SourceLeaf", label=label or expr.measure, details=details
                )
            )
            return node_id
        if isinstance(expr, ScopedAggregateExpr):
            source_id = _next_id("source_leaf")
            measure = _measure_index(config).get(expr.measure)
            nodes.append(
                SemanticDagNode(
                    id=source_id,
                    kind="SourceLeaf",
                    label=label or expr.measure,
                    details={
                        "measure": expr.measure,
                        "aggregation": expr.aggregation
                        or (measure.default_aggregation if measure else ""),
                        "entity": measure.entity if measure else "",
                        "where": [dict(item) for item in expr.where],
                    },
                )
            )
            current = source_id
            for predicate in expr.predicates:
                predicate_id = _next_id("predicate_set")
                nodes.append(
                    SemanticDagNode(
                        id=predicate_id,
                        kind="PredicateSet",
                        label=str(
                            predicate.get("metric", predicate.get("metric", "metric_predicate"))
                        ),
                        inputs=[],
                        details=dict(predicate),
                    )
                )
                semi_id = _next_id("scoped_semijoin")
                nodes.append(
                    SemanticDagNode(
                        id=semi_id,
                        kind="ScopedSemiJoin",
                        inputs=[current, predicate_id],
                        details={
                            "entity": predicate.get("entity", ""),
                            "time_alignment": predicate.get("time_alignment", ""),
                        },
                    )
                )
                current = semi_id
            return current
        if isinstance(expr, MetricRecipeRefExpr):
            recipe = _recipe_index(config).get(expr.metric_recipe)
            if recipe is not None:
                child = _walk(recipe.expression, label=expr.metric_recipe)
                node_id = _next_id("metric_ref")
                nodes.append(
                    SemanticDagNode(
                        id=node_id,
                        kind="MetricRef",
                        label=expr.metric_recipe,
                        inputs=[child],
                        details={"metric": expr.metric_recipe},
                    )
                )
                return node_id
            node_id = _next_id("metric_ref")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="MetricRef",
                    label=expr.metric_recipe,
                    details={"metric": expr.metric_recipe},
                )
            )
            return node_id
        if isinstance(expr, RatioExpr):
            left = _walk(expr.numerator, label=f"{label}_numerator")
            right = _walk(expr.denominator, label=f"{label}_denominator")
            node_id = _next_id("post_calc")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="PostAggregateCalc",
                    label=label,
                    inputs=[left, right],
                    details={"op": "ratio"},
                )
            )
            return node_id
        if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
            left = _walk(expr.left, label=label)
            right = _walk(expr.right, label=label)
            node_id = _next_id("post_calc")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="PostAggregateCalc",
                    label=label,
                    inputs=[left, right],
                    details={"op": expr.op},
                )
            )
            return node_id
        if isinstance(expr, EntityValueExpr):
            child = _walk(expr.input, label=label)
            node_id = _next_id("entity_rollup")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="EntityRollup",
                    label=label,
                    inputs=[child],
                    details={
                        "entity": expr.entity,
                        "value_filters": [dict(item) for item in expr.where],
                    },
                )
            )
            return node_id
        if isinstance(expr, DistributionExpr):
            child = _walk(expr.over, label=label)
            node_id = _next_id("distribution")
            dist_details: dict[str, Any] = {
                "function": expr.function,
                "parameters": dict(expr.parameters),
            }
            if expr.p is not None:
                dist_details["p"] = expr.p
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="DistributionAggregate",
                    label=label,
                    inputs=[child],
                    details=dist_details,
                )
            )
            return node_id
        if isinstance(expr, (BooleanExpr, CallExpr)):
            child_ids = [_walk(arg, label=label) for arg in expr.args]
            node_id = _next_id("post_calc")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="PostAggregateCalc",
                    label=label,
                    inputs=child_ids,
                    details={"op": getattr(expr, "op", getattr(expr, "name", ""))},
                )
            )
            return node_id
        node_id = _next_id(expr_kind(expr).lower())
        nodes.append(
            SemanticDagNode(
                id=node_id,
                kind=expr_kind(expr),
                label=label,
                details={"expression": expr_to_dict(expr)},
            )
        )
        return node_id

    for item in query.select:
        if item.expression is not None:
            outputs[item.as_] = _walk(item.expression, label=item.as_)
    if len(outputs) > 1:
        nodes.append(
            SemanticDagNode(
                id="multi_fact_align",
                kind="MultiFactAlign",
                inputs=list(outputs.values()),
                details={"keys": [*query.group_by, time_output_alias(query.time)]},
            )
        )
    nodes.append(
        SemanticDagNode(
            id="final_project",
            kind="FinalProject",
            inputs=list(outputs.values()),
            details={"outputs": dict(outputs)},
        )
    )
    return SemanticDag(version=1, nodes=nodes, outputs=outputs)
