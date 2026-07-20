"""Frozen dataclasses for the loaded ``PackageConfig`` tree.

This is the typed in-memory representation of a parsed package —
entities, dimensions, measures, metric recipes, relationships,
segments, semantic policies, temporal roles, value domains. Loaded by
:func:`semantic_rails.config.load_package_config` from YAML, consumed
by the compiler and metadata surfaces. All dataclasses are frozen so
the loaded config can be safely shared across threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .expressions import SemanticExpr


@dataclass(frozen=True)
class SeedSpec:
    kind: str = ""
    source: str = ""
    post_sql: str = ""
    null_strings: list[str] = field(default_factory=lambda: [""])


@dataclass(frozen=True)
class ConnectionSpec:
    kind: str = ""
    name: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannerConfig:
    """Per-package settings for the intent planner.

    Read from the ``planner:`` sub-block of the package YAML. Today it
    only carries a pattern-disable list; new planner-related config
    should grow here instead of polluting ``PackageMeta``'s top-level
    fields.
    """

    # Names of intent patterns to skip when composing. Lets a package
    # opt out of a built-in pattern that conflicts with its domain
    # (e.g. a finance package that doesn't want the adoption-funnel
    # pattern). Read by the planner orchestrator. Unknown names are
    # silently ignored — the orchestrator filters by name match only.
    disabled_patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PackageMeta:
    package_id: str
    name: str
    description: str
    warehouse: str = "duckdb"
    default_db: str = ""
    seed: SeedSpec = field(default_factory=SeedSpec)
    connection: ConnectionSpec = field(default_factory=ConnectionSpec)
    environments: list[str] = field(default_factory=list)
    schema_strict: bool = False
    planner: PlannerConfig = field(default_factory=PlannerConfig)


ENTITY_KIND_REGULAR = "regular"
ENTITY_KIND_TIME = "time"
VALID_ENTITY_KINDS = frozenset({ENTITY_KIND_REGULAR, ENTITY_KIND_TIME})

MODEL_KIND_MODEL = "model"
MODEL_KIND_FACT = "fact"
VALID_MODEL_KINDS = frozenset({MODEL_KIND_MODEL, MODEL_KIND_FACT})


@dataclass(frozen=True)
class EntityConfig:
    id: str
    table: str
    primary_key: str
    kind: str = ENTITY_KIND_REGULAR
    relation_id: str = ""
    key: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    key_roles: dict[str, str] = field(default_factory=dict)
    foreign_keys: dict[str, list[str]] = field(default_factory=dict)
    foreign_key_roles: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""
    topics: list[str] = field(default_factory=list)
    calendar_id: str = ""
    allowed_as_root: bool = True
    freshness_source: str = ""
    freshness_sla_seconds: int | None = None
    freshness_as_of: str = ""
    disallowed_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DimensionConfig:
    id: str
    entity: str
    column: str
    data_type: str
    aliases: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""
    semantic_kind: str = ""
    topics: list[str] = field(default_factory=list)
    preferred_filter_ops: list[str] = field(default_factory=list)
    sample_values_strategy: str = ""
    filterable: bool = True
    groupable: bool = True
    value_domain: str = ""


@dataclass(frozen=True)
class TemporalRoleConfig:
    id: str
    dimension: str
    temporal_class: str
    aliases: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    supported_grains: list[str] = field(default_factory=list)
    default_query_time_axis: bool = False
    timezone: str = "UTC"
    column_timezone: str = ""


@dataclass(frozen=True)
class RelationshipConfig:
    id: str
    source_entity: str
    target_entity: str
    source_column: str
    target_column: str
    cardinality: str
    safety: str
    source_columns: list[str] = field(default_factory=list)
    target_columns: list[str] = field(default_factory=list)
    source_key_role: str = ""
    target_key_role: str = ""
    aliases: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""
    path_preference: int = 100
    allowed_directions: list[str] = field(default_factory=lambda: ["forward", "reverse"])
    temporal_validity: dict[str, str] = field(default_factory=dict)
    target_key_type: str = "primary"
    join_semantics: str = ""
    rollup_safe_aggregations: list[str] = field(default_factory=list)
    rollup_safe_aggregations_reverse: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValueDomainValue:
    value: Any
    label: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""


@dataclass(frozen=True)
class ValueDomainConfig:
    id: str
    dimensions: list[str]
    values: list[ValueDomainValue] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class AccumulationConfig:
    """Accumulation semantics for a measure.

    `kind` is one of: "" (default — flow), "flow", "stock", "event",
    "population".
    `snapshot` applies only when kind == "stock"; values are
    "" (default — end_of_period), "start_of_period", "end_of_period".
    """

    kind: str = ""
    snapshot: str = ""


@dataclass(frozen=True)
class MeasureValidityWindow:
    from_: str = ""
    to: str = ""
    semantics: str = ""


@dataclass(frozen=True)
class MeasureExternalDiscontinuity:
    from_: str = ""
    to: str = ""
    what: str = ""
    magnitude_estimate_pct: float | None = None


@dataclass(frozen=True)
class MeasureConfig:
    id: str
    entity: str
    subject_entity: str
    aggregation_entity: str
    row_grain: list[str]
    expr: SemanticExpr
    default_aggregation: str
    allowed_aggregations: list[str]
    source_relation: str = ""
    invalid_aggregations: list[str] = field(default_factory=list)
    measure_class: str = "additive"
    accumulation: AccumulationConfig = field(default_factory=AccumulationConfig)
    compatible_temporal_roles: list[str] = field(default_factory=list)
    value_type: str = "number"
    currency: str = ""
    aliases: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""
    topics: list[str] = field(default_factory=list)
    suggested_aggregations: list[str] = field(default_factory=list)
    comparison_family: str = ""
    comparison_mode: str = ""
    comparison_peers: list[str] = field(default_factory=list)
    clock_variants: list[str] = field(default_factory=list)
    preferred_companion_metrics: list[str] = field(default_factory=list)
    operational: dict[str, Any] = field(default_factory=dict)
    default_temporal_role: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    example_entries: list[dict[str, Any]] = field(default_factory=list)
    validity_windows: list[MeasureValidityWindow] = field(default_factory=list)
    external_discontinuities: list[MeasureExternalDiscontinuity] = field(default_factory=list)
    cross_window_policy: str = "caveat"
    authoring_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MetricConfig:
    id: str
    kind: str
    expression: SemanticExpr
    temporal_role: str = ""
    compatible_temporal_roles: list[str] = field(default_factory=list)
    filter_spec: dict[str, Any] = field(default_factory=dict)
    window_spec: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""
    topics: list[str] = field(default_factory=list)
    comparison_family: str = ""
    comparison_mode: str = ""
    comparison_peers: list[str] = field(default_factory=list)
    clock_variants: list[str] = field(default_factory=list)
    preferred_companion_metrics: list[str] = field(default_factory=list)
    operational: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    example_entries: list[dict[str, Any]] = field(default_factory=list)
    value_type: str = "number"


@dataclass(frozen=True)
class SegmentConfig:
    id: str
    entity: str
    basis_metric: str
    preview_dimensions: list[str] = field(default_factory=list)
    where: list[dict[str, Any]] = field(default_factory=list)
    metric_filters: list[dict[str, Any]] = field(default_factory=list)
    time: dict[str, Any] = field(default_factory=dict)
    temporal_role_overrides: dict[str, str] = field(default_factory=dict)
    path_policy: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""
    topics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PathPreferenceConfig:
    source_entity: str
    target_entity: str
    relationship_path: list[str]


# Hop ceiling applied when a package does not declare one. Four
# relationships covers a five-entity chain (fact → 4 dimensions away),
# which is the deepest traversal we consider safe without the author
# explicitly opting in via ``graph.path_policy.max_hops``.
DEFAULT_PATH_HOP_LIMIT = 4

# Hard upper bound on the author-declared hop ceiling. Path enumeration
# is exponential in the hop limit on dense graphs; past this depth the
# right tool is an authored mapping table or aggregate relation, not a
# longer join chain.
MAX_PATH_HOP_LIMIT = 8


@dataclass(frozen=True)
class PathPolicyConfig:
    """Package-level join-path policy, from ``graph.path_policy:``.

    ``max_hops`` bounds how many relationships a single entity-to-entity
    traversal may cross. Raising it past the default is an explicit
    author decision: long chains are legal and correctness-checked per
    hop, but they are also the queries the acceleration layer will want
    to see (colocation / rollup / mapping-table candidates).
    """

    max_hops: int = DEFAULT_PATH_HOP_LIMIT


@dataclass(frozen=True)
class SemanticPolicyConfig:
    id: str
    kind: str
    config: dict[str, Any] = field(default_factory=dict)
    object_ids: list[str] = field(default_factory=list)
    audiences: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    action: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class SemanticCaveatConfig:
    id: str
    kind: str
    message: str
    object_ids: list[str] = field(default_factory=list)
    entity_values: list[dict[str, Any]] = field(default_factory=list)
    time: dict[str, str] = field(default_factory=dict)
    audiences: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    severity: str = "warning"
    owner: str = ""
    references: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AggregateRelationConfig:
    id: str
    relation: str
    source_entity: str
    measures: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    temporal_role: str = ""
    grain: str = ""
    entity_grain: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    freshness_source: str = ""
    freshness_sla_seconds: int | None = None
    freshness_as_of: str = ""
    model_id: str = ""
    variant_id: str = ""
    source: str = "default"
    time_column: str = ""
    eligible_time_grains: list[str] = field(default_factory=list)
    measure_columns: dict[str, str] = field(default_factory=dict)
    measure_rollups: dict[str, str] = field(default_factory=dict)
    measure_aggregations: dict[str, str] = field(default_factory=dict)
    dimension_columns: dict[str, str] = field(default_factory=dict)
    excluded_entities: list[str] = field(default_factory=list)
    excluded_dimensions: list[str] = field(default_factory=list)
    selection_priority: int = 0
    equivalence_kind: str = ""


@dataclass(frozen=True)
class RelationPipelineStep:
    kind: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelationConfig:
    id: str
    output_name: str
    steps: list[RelationPipelineStep] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    name: str = ""
    label: str = ""
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackageConfig:
    version: int
    package: PackageMeta
    entities: list[EntityConfig]
    dimensions: list[DimensionConfig]
    temporal_roles: list[TemporalRoleConfig]
    relationships: list[RelationshipConfig]
    value_domains: list[ValueDomainConfig]
    measures: list[MeasureConfig]
    metric_recipes: list[MetricConfig]
    segments: list[SegmentConfig] = field(default_factory=list)
    path_preferences: list[PathPreferenceConfig] = field(default_factory=list)
    path_policy: PathPolicyConfig = field(default_factory=PathPolicyConfig)
    semantic_policies: list[SemanticPolicyConfig] = field(default_factory=list)
    semantic_caveats: list[SemanticCaveatConfig] = field(default_factory=list)
    aggregate_relations: list[AggregateRelationConfig] = field(default_factory=list)
    relations: list[RelationConfig] = field(default_factory=list)
    operational_contract: dict[str, Any] = field(default_factory=dict)
    meta_contract: dict[str, Any] = field(default_factory=dict)
