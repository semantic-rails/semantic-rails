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

    def add(
        prefix: str, kind: str, label: str, details: dict[str, Any], inputs: list[str] | None = None
    ) -> str:
        node_id = _next_id(prefix)
        nodes.append(
            SemanticDagNode(
                id=node_id, kind=kind, label=label, inputs=list(inputs or []), details=details
            )
        )
        return node_id

    def _walk(expr: SemanticExpr, *, label: str = "") -> str:
        if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
            measure = _measure_index(config).get(expr.measure)
            details: dict[str, Any] = {
                "measure": expr.measure,
                "aggregation": expr.aggregation or (measure.default_aggregation if measure else ""),
                "entity": measure.entity if measure else "",
            }
            if isinstance(expr, ScopedAggregateExpr):
                details["where"] = [dict(item) for item in expr.where]
            else:
                details["temporal_roles"] = list(
                    measure.compatible_temporal_roles if measure else []
                )
            if isinstance(expr, AggregateExpr):
                details["filter"] = dict(expr.filter)
            current = add("source_leaf", "SourceLeaf", label or expr.measure, details)
            for predicate in expr.predicates if isinstance(expr, ScopedAggregateExpr) else ():
                predicate_id = add(
                    "predicate_set",
                    "PredicateSet",
                    str(predicate.get("metric", "metric_predicate")),
                    dict(predicate),
                )
                current = add(
                    "scoped_semijoin",
                    "ScopedSemiJoin",
                    "",
                    {
                        "entity": predicate.get("entity", ""),
                        "time_alignment": predicate.get("time_alignment", ""),
                    },
                    [current, predicate_id],
                )
            return current
        if isinstance(expr, MetricRecipeRefExpr):
            recipe = _recipe_index(config).get(expr.metric_recipe)
            inputs = (
                [_walk(recipe.expression, label=expr.metric_recipe)] if recipe is not None else []
            )
            details = {"metric": expr.metric_recipe}
            return add("metric_ref", "MetricRef", expr.metric_recipe, details, inputs)
        if isinstance(expr, RatioExpr):
            left = _walk(expr.numerator, label=f"{label}_numerator")
            right = _walk(expr.denominator, label=f"{label}_denominator")
            return add("post_calc", "PostAggregateCalc", label, {"op": "ratio"}, [left, right])
        if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
            left = _walk(expr.left, label=label)
            right = _walk(expr.right, label=label)
            return add("post_calc", "PostAggregateCalc", label, {"op": expr.op}, [left, right])
        if isinstance(expr, EntityValueExpr):
            child = _walk(expr.input, label=label)
            details = {
                "entity": expr.entity,
                "value_filters": [dict(item) for item in expr.where],
            }
            return add("entity_rollup", "EntityRollup", label, details, [child])
        if isinstance(expr, DistributionExpr):
            child = _walk(expr.over, label=label)
            details = {"function": expr.function, "parameters": dict(expr.parameters)}
            if expr.p is not None:
                details["p"] = expr.p
            return add("distribution", "DistributionAggregate", label, details, [child])
        if isinstance(expr, (BooleanExpr, CallExpr)):
            child_ids = [_walk(arg, label=label) for arg in expr.args]
            details = {"op": getattr(expr, "op", getattr(expr, "name", ""))}
            return add("post_calc", "PostAggregateCalc", label, details, child_ids)
        details = {"expression": expr_to_dict(expr)}
        return add(expr_kind(expr).lower(), expr_kind(expr), label, details)

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
