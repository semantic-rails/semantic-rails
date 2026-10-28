"""Frozen dataclasses for the loaded ``PackageConfig`` tree.

This is the typed in-memory representation of a parsed package —
entities, dimensions, measures, metric recipes, relationships,
segments, semantic policies, temporal roles, value domains. Loaded by
:func:`semantic_rails.config.load_package_config` from YAML, consumed
by the compiler and metadata surfaces. All dataclasses are frozen so
the loaded config can be safely shared across threads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import SemanticLayerError
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


# Where a sum or count is judged to have data, so its empty groups read 0 (see empty_groups).
OBSERVATION_SCOPES = ("dataset", "query")


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
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    observation_scope: str = OBSERVATION_SCOPES[0]  # defaults.observation_scope


ENTITY_KIND_REGULAR = "regular"


@dataclass(frozen=True)
class EntityConfig:
    id: str
    table: str
    # Empty for a row-grain model: its rows have no declared key, and every stored row counts.
    key_columns: list[str] = field(default_factory=list)
    kind: str = ENTITY_KIND_REGULAR
    relation_id: str = ""
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
    # The model authors `entities: {bridge: true}`: a declared link table routes may pass through.
    bridge: bool = False
    # The dimension (on the entity's own model, not its key) that names one row in an answer.
    display: str = ""
    # A row-grain entity's model (its rows have no key, so it names nothing else).
    model: str = ""
    # The model authors `one_per_key: true`: its relation is read as one row per key, each
    # column the key's one value (NULL where its rows disagree). ``table`` is that reading.
    one_per_key: bool = False

    @property
    def is_row_grain(self) -> bool:
        return not self.key_columns

    @property
    def key(self) -> list[str]:
        """The key columns. A row-grain model has none, so a read that must tell its rows apart
        refuses here instead of reading an empty key."""
        if not self.key_columns:
            model = self.model or self.id
            raise SemanticLayerError(
                "ROW_GRAIN_UNSUPPORTED",
                f"'{self.label or model}' is row grain (no key), and this needs to tell its rows "
                "apart (a count of them as entities, a segment, a predicate, a conversion or a "
                "stock). Count its rows, sum its values, or group by its or its parents' "
                f"columns; or declare a key on {model} if its rows have one.",
                details={"model": model, "entity": self.id},
            )
        return list(self.key_columns)

    @property
    def primary_key(self) -> str:
        return self.key[0]


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
    sample_values_strategy: str = ""
    filterable: bool = True
    groupable: bool = True
    value_domain: str = ""
    # Values the source writes for "unknown" (``''``, ``'XX'``): read as NULL wherever the
    # dimension is read, so grouping, ``missing`` and ``not`` treat them as unknown.
    unknown_values: list[Any] = field(default_factory=list)


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
    allowed_directions: list[str] = field(default_factory=lambda: ["forward", "reverse"])
    temporal_validity: dict[str, str] = field(default_factory=dict)
    target_key_type: str = "primary"
    join_semantics: str = ""
    rollup_safe_aggregations_reverse: list[str] = field(default_factory=list)
    # Derived, never authored: a row-grain model's foreign key to the one-per-key model over the
    # same relation, on its key. Walked in reverse, it reaches the columns that can hold several
    # values per key, each key counting once in every group it has a row in.
    link: bool = False


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
class ZeroThrough:
    """How far a table counts as loaded, so its empty periods read 0 up to there (a model's or
    the project's ``zero_through``; ``data``, the last row, is no record). ``now``: through the
    request's ``now``. ``loaded``: through ``MAX(column)`` of ``relation``, the date or
    timestamp a load records as complete."""

    kind: str  # "now" | "loaded"
    name: str = ""  # "<model>.<field>", as the package declares it
    relation: str = ""
    column: str = ""
    data_type: str = ""  # "date" | "timestamp"
    timezone: str = "UTC"  # the zone a naive timestamp column is stored in


@dataclass(frozen=True)
class MeasureConfig:
    id: str
    entity: str
    row_grain: list[str]
    expr: SemanticExpr
    default_aggregation: str
    allowed_aggregations: list[str]
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
    preferred_companion_metrics: list[str] = field(default_factory=list)
    operational: dict[str, Any] = field(default_factory=dict)
    default_temporal_role: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    example_entries: list[dict[str, Any]] = field(default_factory=list)
    validity_windows: list[MeasureValidityWindow] = field(default_factory=list)
    external_discontinuities: list[MeasureExternalDiscontinuity] = field(default_factory=list)
    cross_window_policy: str = "caveat"
    authoring_warnings: list[str] = field(default_factory=list)
    # False for already-aggregated values (a vendor's distinct count, a ratio): the
    # engine never sums two of them.
    additive: bool = True
    # ``kind: lookup``: the measure totalled per ``via`` entity and carried onto this
    # measure's rows; ``expr`` is the foreign key to ``via``.
    lookup_from: str = ""
    lookup_via: str = ""
    # False when authored ``publish: false`` and no metric aggregates it whole; a metric that
    # filters it then makes it a building block (``config_parts.measure_governance``).
    publish: bool = True
    # Its model's declared loaded edge; None is the last row (``data``).
    zero_through: ZeroThrough | None = None


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
    # What the route means in business words (``graph.path_preferences[].label``).
    label: str = ""


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


@dataclass(frozen=True)
class AggregateRelationConfig:
    id: str
    relation: str
    source_entity: str
    measures: list[str] = field(default_factory=list)
    # Every dimension the table may be grouped by; a query reads only `dimension_columns`.
    dimensions: list[str] = field(default_factory=list)
    temporal_role: str = ""
    grain: str = ""
    entity_grain: list[str] = field(default_factory=list)
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
    # What each measure column holds per row (`holds:`); empty when not declared.
    measure_holds: dict[str, str] = field(default_factory=dict)
    dimension_columns: dict[str, str] = field(default_factory=dict)
    # The relationship path a pre-joined dimension column was built along (`path:`).
    dimension_paths: dict[str, list[str]] = field(default_factory=dict)
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


@dataclass(frozen=True, kw_only=True)
class VisiblePackageConfig(PackageConfig):
    """A caller view whose provenance survives dataclass replacement."""

    view: Any


def require_base(config: PackageConfig) -> None:
    if isinstance(config, VisiblePackageConfig):
        raise TypeError("Policy enforcement takes the whole package, never a visible view.")


def base_of(config: PackageConfig) -> PackageConfig:
    return config.view.base if isinstance(config, VisiblePackageConfig) else config


def require_boolean_mnpi(meta: Mapping[str, Any], where: str) -> None:
    """``meta.mnpi`` is ``true`` or ``false``: any other value refuses the package."""
    if "mnpi" in meta and not isinstance(meta["mnpi"], bool):
        raise SemanticLayerError("INVALID_CONFIG", f"{where}: meta.mnpi must be true or false")


def require_boolean_mnpi_package(config: PackageConfig) -> None:
    """:func:`require_boolean_mnpi` for every record of a package built in code."""
    rows: list[MeasureConfig | MetricConfig | RelationConfig] = [
        *config.measures,
        *config.metric_recipes,
        *config.relations,
    ]
    for row in rows:
        require_boolean_mnpi(row.meta, row.id)
