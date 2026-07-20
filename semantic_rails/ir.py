"""Logical-plan dataclasses produced by the compiler.

:class:`BoundMeasure`, :class:`PathSelection`, :class:`LogicalPlan`,
:class:`ValidationReport`, and the per-stage explain payload. The
compiler builds these; ``validate`` / ``explain`` serialize them.
Frozen on purpose — the logical plan is treated as an immutable
intermediate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BoundMeasure:
    measure_id: str
    aggregation: str
    alias: str
    temporal_role: str
    aggregation_params: dict[str, Any] = field(default_factory=dict)
    filter_spec: dict[str, Any] = field(default_factory=dict)
    window_spec: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PathSelection:
    target_entity: str
    purpose: str
    chosen_path: list[str]
    candidate_paths: list[list[str]]
    analysis: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RewriteStep:
    kind: str
    status: str
    measure_id: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MeasurePlan:
    cte_name: str
    bound_measure: BoundMeasure
    source_entity: str
    path_selections: list[PathSelection] = field(default_factory=list)
    grain_keys: list[str] = field(default_factory=list)
    rewrite_strategy: str = "direct"
    required_entities: list[str] = field(default_factory=list)
    aggregate_relation_id: str = ""


@dataclass(frozen=True)
class SemanticDagNode:
    id: str
    kind: str
    label: str = ""
    inputs: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticDag:
    version: int = 1
    nodes: list[SemanticDagNode] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PhysicalPlanNode:
    id: str
    kind: str
    label: str = ""
    inputs: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PhysicalPlan:
    version: int = 1
    nodes: list[PhysicalPlanNode] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    optimizations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PerformancePlan:
    version: int = 1
    estimated_scan_relations: list[dict[str, Any]] = field(default_factory=list)
    raw_fact_count: int = 0
    date_filter_pushdown: list[dict[str, Any]] = field(default_factory=list)
    pre_aggregation_grain: dict[str, list[str]] = field(default_factory=dict)
    joins_before_aggregate: list[dict[str, Any]] = field(default_factory=list)
    joins_after_aggregate: list[dict[str, Any]] = field(default_factory=list)
    full_outer_alignments: int = 0
    large_scan_risk: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    execution_recommendation: str = "safe_to_run"
    semantic_equivalence: str = "exact_primitives"
    notes: list[str] = field(default_factory=list)
    aggregate_routing: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LogicalPlan:
    version: int
    query: dict[str, Any]
    root_entity: str
    resolved_ids: dict[str, Any]
    selected_paths: dict[str, list[str]]
    candidate_paths: dict[str, list[list[str]]]
    group_by: list[str]
    time: dict[str, Any]
    bound_measures: list[BoundMeasure]
    measure_plans: list[MeasurePlan]
    post_aggregation_exprs: dict[str, dict[str, Any]]
    grain_analysis: dict[str, Any]
    fanout_strategy: dict[str, Any]
    rewrite_steps: list[RewriteStep] = field(default_factory=list)
    disabled_options: list[dict[str, Any]] = field(default_factory=list)
    semantic_dag: SemanticDag = field(default_factory=SemanticDag)
    # Per-query synthetic measures produced by binding-time rewrites
    # (e.g. ``aggregate_if`` lifts each ConditionalAggregateExpr to an
    # anonymous MeasureConfig). These are not part of the package
    # config — every lookup site that previously did
    # ``measures = _measure_index(config); measures[id]`` must now use
    # ``measures_with_synthetics(plan, config)`` instead so the
    # synthetic ids resolve. Frozen + Any to avoid an ir.py↔schema.py
    # import cycle; downstream calls treat values as MeasureConfig.
    synthetic_measures: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExplainArtifact:
    normalized_query: dict[str, Any]
    alias_resolution: dict[str, Any]
    chosen_paths: dict[str, Any]
    grain_analysis: dict[str, Any]
    fanout_strategy: dict[str, Any]
    rewrite_strategy: dict[str, Any]
    logical_plan: dict[str, Any]
    semantic_dag: dict[str, Any]
    physical_plan: dict[str, Any]
    performance_plan: dict[str, Any]
    sql_plan: dict[str, Any]
    rendered_sql: str
    compile_stats: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationReport:
    version: int
    ok: bool
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    disabled_options: list[dict[str, Any]] = field(default_factory=list)
    logical_plan: dict[str, Any] = field(default_factory=dict)
    explain: dict[str, Any] = field(default_factory=dict)
