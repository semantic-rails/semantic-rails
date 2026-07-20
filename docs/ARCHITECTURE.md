# Semantic Layer Architecture Spec

This document is the architecture spec for the active `semantic_rails` runtime.

Current package authoring uses the graph-first directory format described in [PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md). All packages use the ergonomic `schema_version: 1` authoring contract; the loader normalizes the YAML into the internal `PackageConfig` runtime contract.

## Active Package Contract

The loader expects package directories shaped like:

```text
configs/semantic_rails/<package>/
  package.yml
  graph.yml
  defaults.yml          # optional
  policies.yml          # optional
  caveats.yml           # optional
  metrics.yml           # optional
  examples/             # optional
  tests/                # optional
  models/
    **/*.yml
  metrics/
    **/*.yml
```

Key authoring principles:

- `graph.yml` is the source of truth for entity identity, canonical keys, and allowed roots
- `models/**` is the primary authoring unit for marts and tables
- `metrics/**` declares governed access patterns (ratios, derived, cumulative, rolling, prior-period, period-to-date, conversion); not every measure needs a metric
- measures are primitive business quantities. They are queryable directly through `select.expression.measure`; they do not auto-publish into the metric catalog. Metrics are explicit, governed contracts.
- joins are mostly implicit from FK-to-entity-key mappings
- temporal-validity joins, non-PK targets, and other non-default cases stay explicit
- `model.variants:` can describe alternate physical rollups for the same
  semantic model; the loader normalizes those into aggregate relations used by
  the compiler

## Scope

- Runtime package: `semantic_rails/`
- Supported package configs: `configs/semantic_rails/<package>/package.yml`
- Active package: [configs/semantic_rails/jaffle_shop](../configs/semantic_rails/jaffle_shop)
- Focused semantic tests: `tests/semantic_rails/`

## Architecture Principles

- AST first: query input is normalized before planning, and SQL is rendered only after lowering from typed semantic structures.
- Stable IDs are canonical. `name`, `label`, and inline synonyms are discovery inputs only.
- Time is first-class and modeled with explicit `TemporalRole` objects.
- Measures are durable configured primitives. Metrics are governed access patterns built from measures and other semantic objects.
- Fanout protection belongs to the compiler, not to callers.
- Metadata must be query-state aware, expose a stable builder-first contract, and answer `build-options` and `valid-values`.
- Explain output is part of the product, not a debugging afterthought.
- DuckDB is the zero-setup local execution target; Snowflake execution is available through Snow CLI or optional native connector adapters when the package declares a configured connection.
- Physical routing is semantic-first. The compiler may use exact aggregate
  relations for efficiency, but only when the configured rollup covers the
  requested measures, dimensions, filters, time role, and time grain.

## Semantic Primitives

### Entity

A business object with identity.

Current compiled fields:

- `id`
- `name`
- `label`
- `table`
- `key`
- `allowed_as_root`
- optional `calendar_id`

Notes:

- entity keys are ordered lists and may be compound
- entity identity is declared in `graph.yml`

### Dimension

A filterable or groupable attribute attached to an entity.

Current compiled fields:

- `id`
- `name`
- `label`
- `entity`
- `column`
- `data_type`
- `groupable`
- `filterable`
- optional `value_domain`

### TemporalRole

A semantic time axis such as event time, state time, validity time, calendar time, or as-of time.

Current compiled fields:

- `id`
- `name`
- `label`
- `dimension`
- `temporal_class`
- `supported_grains`
- `default_query_time_axis`
- `timezone`

### Measure

A quantitative primitive defined over a dataset and a business grain.

Current compiled fields:

- `id`
- `name`
- `label`
- `entity`
- `subject_entity`
- `aggregation_entity`
- `row_grain`
- `expr`
- `default_aggregation`
- `allowed_aggregations`
- `invalid_aggregations`
- `measure_class` (derived from `accumulation.kind`; not authored directly)
- `accumulation` (`{ kind: flow | stock | event | population, snapshot?: start_of_period | end_of_period }`)
- `compatible_temporal_roles`
- `value_type`
- optional `currency`

Measure rules:

- measures are modeled in the owning model file
- `expr` is a typed AST mapping or simple arithmetic string parsed into AST
- there is no `primitive:` shorthand and no separate `snapshot_policy:` field; authors declare an explicit `accumulation:` block. For stock-like measures, the snapshot policy lives nested as `accumulation: { kind: stock, snapshot: end_of_period }`.
- `accumulation: { kind: stock }` lowers to semi-additive behavior; `disallowed_aggregations:` removes any aggregation kind that does not make business sense for the measure.

### Relationship

A governed path between entities.

Current compiled fields:

- `id`
- `name`
- `label`
- `source_entity`
- `target_entity`
- `source_columns`
- `target_columns`
- `cardinality`
- `safety` (`safe | requires_rewrite | unsafe`)
- `allowed_directions`
- optional `temporal_validity`

Relationship rules:

- most relationships are inferred from model-local FK mappings to graph entity keys
- compound-key joins are first-class
- temporal-validity windows are explicit when needed

### AggregateRelation

An exact physical rollup relation that can serve a compatible measure leaf.
Authors usually define these through `model.variants:`; the loader normalizes
eligible non-transaction variants into this compiled shape.

Current compiled fields:

- `id`
- `relation`
- `source_entity`
- `model_id`
- `variant_id`
- `source`
- `temporal_role`
- `time_column`
- `grain`
- `eligible_time_grains`
- `entity_grain`
- `measures`
- `measure_columns`
- `measure_rollups`
- `measure_aggregations`
- `dimensions`
- `dimension_columns`
- `excluded_entities`
- `excluded_dimensions`
- `selection_priority`
- `equivalence_kind`
- optional freshness metadata

Aggregate relation rules:

- the MVP only accepts `source: default`; cross-warehouse routing is future work
- `equivalence_kind` must be `exact` for automatic routing
- the rollup grain must be at or below the requested query grain
- all selected measures, grouped dimensions, and filtered dimensions must be
  covered by the relation
- unsupported rollups fall back to the raw model relation rather than compiling
  an unsafe shortcut

### ValueDomain

A governed set of valid values for a dimension.

Current compiled fields:

- `id`
- `name`
- `label`
- `dimensions`
- `values`

### Metric

A governed access pattern over measures and other semantic primitives. Metrics
are explicit named contracts; measures stay queryable directly without needing
a metric wrapper.

Current compiled fields:

- `id`
- `name`
- `label`
- `kind`
- `expression`
- `temporal_role`
- `compatible_temporal_roles`

Current metric kinds demonstrated in the active package:

- aggregate
- ratio
- derived
- cumulative
- rolling
- prior-period
- period-to-date
- semi-additive
- planner-executed event-pair conversion metrics for the supported event-count model
- clock-variant and comparison-family metadata used by discovery and planning

## Query Contract

The public query payload is AST-native and versioned.

Top-level fields:

- `version`
- `select`
- `group_by`
- `where`
- `metric_filters`
- `time`
- `temporal_role_overrides`
- `path_policy`
- `order_by`
- `limit`
- `debug`
- `explain`

Core query rules:

- `select` supports measure references, metric references, and derived AST expressions
- `time.temporal_role` must point at a declared temporal role ID
- `time.grain` must be one of the grains declared by the selected temporal role
- `time.fill` triggers dense-series planning against a calendar entity
- `time.calendar_id` selects a declared calendar when more than one exists
- `metric_filters` are applied after projected expressions except for `metric_predicate`, which is planned semantically at entity plus contextual time/group scope
- `temporal_role_overrides` must only reference declared temporal roles

## Expression Surface

### Query-Time

Supported query-time families:

- measure references
- metric references
- arithmetic
- cumulative
- rolling
- prior-period
- period-to-date
- `metric_predicate`
- enriched conversion expressions

Current conversion behavior:

- conversion expressions execute for the supported event-count model
- unsupported conversion shapes still reject explicitly with `CONVERSION_NOT_SUPPORTED`

### Config-Time

Supported config-time measure expression families:

- `column`
- `literal`
- `arithmetic`
- `comparison`
- `boolean`
- `call`
- `case`

## Compiler Pipeline

The runtime compiles a request through these stages:

1. Normalize the incoming query payload into `NormalizedQuery`.
2. Resolve stable IDs, names, labels, and candidate semantic paths.
3. Validate temporal bindings, grain compatibility, and semantic-policy constraints.
4. Build `LogicalPlan` and leaf measure plans.
5. Lower `LogicalPlan` into typed SQL AST.
6. Render SQL text from the SQL AST.
7. Execute the rendered SQL against DuckDB.

Important planner behaviors:

- safe mixed-grain cases compile via leaf pre-aggregation rewrites
- exact aggregate relations can be selected for compatible time-grain measure
  leaves; routed leaves expose `aggregate_relation_id` and physical/performance
  plan metadata
- historical joins use temporal-validity conditions anchored to the effective time axis
- dense fill uses a declared calendar entity
- `metric_predicate` compiles as a scoped predicate subplan rather than a projected boolean expression
- query-time predicates default to contextual scope
- package-authored predicates must declare `scope_mode`
- supported v1 predicate modes are `contextual` and `entity_only`
- contextual predicates inherit outer time and compatible grouped context entities, and inherit compatible filters without widening the join key
- contextual `time_grain` overrides are limited to coarser deterministic ancestor buckets on the same calendar
- supported conversion requests compile as event-pair matching subplans
- unsupported conversion requests fail semantically rather than silently degrading into ratios

### Compile Cache Seam

The runtime memoizes compiled plans behind a cache keyed on a sha256 of the
normalized query, package fingerprint, warehouse, render profile, and policy
context (`semantic_rails.cache.compilation_cache_key`). The default backend is
`LruCompiledSqlCache` — an in-process LRU sized via the
`SEMANTIC_RAILS_COMPILE_CACHE_SIZE` env var (default 512). For the OSS
standalone experience this is sufficient and requires no setup.

`CompiledSqlCache` is deliberately a process-local typed-object contract.
`runtime.set_compile_cache(cache)` supports custom eviction and instrumentation,
and reload preserves the injected backend while the package fingerprint in each
key prevents stale hits. Cached plans contain compiler dataclasses and are not
promised to be JSON serialisable or release-compatible. A horizontally shared
cache therefore requires a separately versioned `CompiledArtifact` codec; Redis
or Memcached adapters must not pickle or stringify the current internal object
graph. This narrow contract keeps the OSS behavior honest while the hosted
acceleration layer defines that durable codec explicitly.

## Metadata Surface

The metadata APIs expose:

- package catalog
- guided discovery and inspection
- `build-options`
- `valid-values`
- deterministic planning
- validation results
- explain artifacts

Important metadata behaviors:

- metrics expose default and compatible temporal roles
- valid and disabled grouping entities are explicit
- provenance is exposed for non-local dimensions
- capabilities distinguish supported and unsupported features with reasons
- comparison families and clock variants are exposed on relevant object cards

## Error Philosophy

The runtime should reject incorrect or unsafe semantics explicitly rather than guessing.

Representative semantic errors:

- `AMBIGUOUS_ALIAS`
- `AMBIGUOUS_PATH`
- `FANOUT_UNSAFE`
- `MIXED_GRAIN_INVALID`
- `REWRITE_NOT_SUPPORTED`
- `INVALID_TEMPORAL_ROLE`
- `INCOMPATIBLE_TEMPORAL_ROLE`
- `INCOMPATIBLE_CALENDAR`
- `INVALID_METRIC_PREDICATE`
- `PREDICATE_GRAIN_UNSAFE`
- `CONVERSION_NOT_SUPPORTED`

## Active Remaining Limits

- DuckDB is the zero-setup local backend; Snowflake execution depends on a configured `snowflake_cli` or `snowflake_native` connection.
- The executed conversion family is intentionally scoped to the supported event-count model rather than a fully general conversion planner.
- `metric_predicate` is implemented for the supported contextual and entity-only cases used by the active package, but it is not yet a fully general arbitrary nested predicate planner.
