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

## Package Upgrade Planning

`semantic_rails.upgrade` plans YAML edits in memory. Finding identities include the
rule, source file and full YAML path; author decisions remain pending until answered.
Duplicate identities, overlapping edits and incompatible list edit orders refuse
with `CONFIG_CONFLICT`. Independent list edits run from higher indexes to lower
indexes, preserving each original target and each finding's own edit sequence.
Source iterators retain loader identities. Expression roots follow the loader:
a truthy `expression` block takes precedence; otherwise only non-null direct
expression fields are walked. Segment membership queries remain expression sources.
Traversal shares the expression validator's child iterator, which visits every key
except opaque metadata, literal/filter values and parameters. Row metadata stays
outside the expression roots. The rule registry is empty; no command invokes it yet.

## Architecture Principles

- AST first: query input is normalized before planning, and SQL is rendered only after lowering from typed semantic structures.
- Stable IDs are canonical. `name`, `label`, and inline synonyms are discovery inputs only.
- Time is first-class and modeled with explicit `TemporalRole` objects.
- Measures are durable configured primitives. Metrics are governed access patterns built from measures and other semantic objects.
- Fanout protection belongs to the compiler, not to callers.
- Metadata must be query-state aware, expose a stable builder-first contract, and answer `build-options` and `valid-values`.
- Explain output is part of the product, not a debugging afterthought.
- DuckDB is the zero-setup local execution target; Snowflake execution is available through Snow CLI or optional native connector adapters when the package declares a configured connection.
- Every engine-owned DuckDB connection, including DuckLake, MotherDuck, seed
  operations and authoring introspection, disables `common_subplan` before running
  warehouse SQL, preserving existing optimizer exclusions. This avoids incorrect
  multi-measure totals over views filtered on derived columns in DuckDB 1.5.6.
- DuckDB utility SQL for connection setup, confinement, CSV loading and authoring
  metadata qualifies built-in functions through `system.main`. Database-file
  macros cannot override those calls. Before setup, connections refuse scalar
  or table macros whose names collide with built-ins listed in the system
  catalog, with `INVALID_CONFIG` and reason `duckdb_builtin_macro_collision`;
  setup failures close the connection rather than continuing without its settings.
  Compiled warehouse queries and the file's own views resolve against the file's
  catalog by design; their boundary is confinement, not name qualification.
- DuckDB, DuckLake, MotherDuck and authoring introspection read materialized
  relations through one shared helper. Result limits use a relation limit and
  one extra row to detect truncation, avoiding DuckDB's streamed cursor fetches
  that can hang on queries with window functions. DuckDB's per-query interrupt
  watchdog also covers materialization.
- Physical routing is semantic-first. The compiler may use exact aggregate
  relations for efficiency, but only when the configured rollup covers the
  requested measures, dimensions, filters, time role, and time grain.
- Modules import only their own or lower layers: entry points, transports, authoring,
  planner, metadata, runtime, compiler, warehouses, package loading, SQL, core.
  `tests/semantic_rails/test_import_layers.py` lists each module's layer and the known
  exceptions and import cycles, which may only shrink.

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
- `default_query_time_axis`: true for the role marked `default: true` on its model
- `timezone`

### Measure

A quantitative primitive defined over a dataset and a business grain.

Current compiled fields:

- `id`
- `name`
- `label`
- `entity`
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
- there is no `primitive:` shorthand and no separate `snapshot_policy:` field; authors declare an explicit `accumulation:` block. Authored model relations use `relation`, dimensions use `kind`, time entries use `class`, metrics use `temporal_role`, and segments use `basis_metric`; the loader refuses alternate spellings as unknown keys. For stock-like measures, the snapshot policy lives nested as `accumulation: { kind: stock, snapshot: end_of_period }`.
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
Authors define these through `model.variants:`; the loader normalizes
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
- the rollup grain must be at or below the requested query grain, its buckets
  must nest in the query's (a week rollup answers only week queries), and the
  query's bounds must fall on its bucket boundaries
- a `count_distinct` routes only when it counts the single-column row key of a
  model that isn't a fact model; relations that declare `filters`, leaves with
  metric predicates, roles that convert time zones and non-default calendars
  don't route
- all selected measures, grouped dimensions, and filtered dimensions must be
  covered by the relation
- on DuckDB and Postgres, filled, dense-series and combined plans read base relations
  to preserve time coverage; candidates report `base_time_coverage_required`
- unsupported rollups fall back to the raw model relation rather than compiling
  an unsafe shortcut

`semantic_rails/acceleration/routing.py` holds the routing kill switch
(`SEMANTIC_RAILS_AGGREGATE_ROUTING`, `Runtime.set_aggregate_routing`), which the
runtime's request scope applies, and builds
`performance_plan.aggregate_routing.candidates` from each measure plan's rejections.

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
- `order_by`
- `limit`
- `debug`
- `explain`

Core query rules:

- `select` supports measure references, metric references, and derived AST expressions
- `time.temporal_role` must point at a declared temporal role ID
- `time.grain` must be one of the grains declared by the selected temporal role
- `time.fill` triggers dense-series planning against the calendar entity for `time.calendar_id`, or,
  for the default calendar when none is declared, an implicit Gregorian day spine generated in SQL
- `time.calendar_id` selects a declared calendar when more than one exists
- a query with a time axis and no explicit `order_by` orders its final SQL projection by
  time ascending, then by `group_by` dimensions ascending in their stated order; this
  shared lowering rule also covers dense series and combined or accelerated plans.
  Internal branches, distribution inputs and contextual predicate sources receive
  no default ordering; explicit `order_by` still takes precedence
- `metric_filters` are applied after projected expressions except for `metric_predicate`, which is planned semantically at entity plus contextual time/group scope
- queries without `select` still apply aggregate `metric_filters` through their measure
  leaves. A `metric_predicate` reaching distinct-value lowering without a measure or
  conversion leaf is refused with `PREDICATE_NOT_SUPPORTED`; add a select that reads a
  measure, or remove `metric_filters`. Ordinary `where`, `order_by` and `limit` still
  apply to distinct-group queries
- `temporal_role_overrides` must only reference declared temporal roles
- an output leaf's bound clock takes precedence over advertised compatible clocks;
  a query that would replace it with another advertised clock refuses with
  `INVALID_TEMPORAL_BINDING`, in both planning and SQL lowering
- when only some measures have the query's clock, each other measure is timed by its own
  clock; one with several clocks, none of them the query's, fails with
  `INCOMPATIBLE_TEMPORAL_ROLE` unless its aggregate's `temporal_role` or
  `temporal_role_overrides` names one (a declared `default_temporal_role` doesn't; conversion
  operands keep their own clock rules)
- a measure with no clock at all (no `times:` of its own and no model `default` time) can't be
  bucketed: any query with `time` that binds it fails with `INCOMPATIBLE_TEMPORAL_ROLE`
  (`details.compatible: []`, hint `declare_measure_time_role`); it still answers without `time`
  or grouped by a plain date dimension; an `aggregate_if` has no clock, so `time` refuses it
  with its own message and no hint (declare a measure with `times:` and aggregate that)

## Expression Surface

Authorization uses the compiler's immutable `BoundQuery`, prepared before SQL
rendering or adapter access. Planning selects the paths; binding constructs the
SQL AST for all selected branches, including conversions and nested predicates.
Recursive distribution and sibling branches use SQL-AST-only preparation in the
same recording scope; they never invoke full compilation. Compiler indexes record
resolved measures, recipes, dimensions, temporal roles,
entities and relationships in a request-local context. The selected plan contributes
its effective time roles and entity/path dependencies. Dimension/role/measure
ownership follows the resolved config objects, rather than request spellings.
Candidate planning is excluded from recording, including recursive plans, so an
unused alternative path does not become an authorization dependency. Calendar and
entity-key bindings selected by column identity record their chosen objects too.
Foreign-key projections record the selected relationship even when lowering
avoids a physical join; rejected relationship candidates remain outside the set.

Runtime validate/compile/query and restricted resource grants enforce the same
bound object set. Supporting metadata uses the same compiler ownership records
for dimensions and roles. Temporal recipe metadata binds a valid default time
invocation using the compiler-selected role and a supported grain; a missing
caller time axis does not hide an otherwise valid granted metric. When all window
leaves resolve to one clock, metadata uses that clock ahead of the advertised
compatible list; otherwise it retains the advertised default. Actual queries
always bind and authorize their own time context. Rendering reuses the authorized plan and SQL AST. Every request
is authorized before consulting the compiled-result cache; policy contexts remain
in cache keys. Segment preview and membership/count query preparations use the
same binding gate. Live values delegate to the governed query path.

The expression visitor checks request shape against the expression parser's own
kind vocabulary (every Query IR kind, config-only leaf and parser alias such as
`nullif` or `not_in`, plus inline-threshold and value-filter data tags) and rejects
unknown explicit kinds before lossy normalization. Caller `policy_context` metadata
is not an expression position. Literal/filter values and scalar parameters are
data: neither nested keys nor reference-shaped strings become expressions or
semantic references.
Lineage and caveat helpers retain the common reference collector; it never
supplies an authorization dependency set. The same compilation records the
objects read by cuts: metric filters and filters embedded in aggregate, scoped
aggregate, conditional aggregate and recipe expressions. An entity-value cut
includes the entity and key dimensions defining its per-entity grain. Filter allowlists use
those bindings, including nested recipes and effective temporal dependencies.
Outer select expressions, grouping, where, time axes and attachment joins do
not become filter dependencies merely because a cut is evaluated in their
context. Constant cuts can resolve with no object reads; unresolved cut bindings
are refused under filter allowlists. Each cut records the leaves it filters; a
governed object is checked against `metric_filters` cuts plus its own leaves' cuts,
and against every cut when ownership is ambiguous. `allow_metric_filters: false`
refuses the cuts that count for it. The fields of the filters the caller wrote inside
expressions are read from the normalized query, owned by the root leaves over their
measure under that same rule (`BoundQuery.cut_counts`), and checked against
`allowed_where`. Fields nested under predicates, `metric_filters` or conversion
operands conservatively count for every governed object in the query; direct
leaf filters retain the leaf rule. Expression filters never satisfy
`required_where`. Temporal-role constraints check the query
axis and each governed object's effective bucket and ordering roles before
rendering.
Caller-created synthetic aggregates inherit the applicable constraints of every
measure reading any of their source columns, including condition columns. Source
resolution is shared with measure SQL lowering, and the existing constraint evaluator
checks each synthetic aggregate's own cuts. Relation and column identifiers match
conservatively across case and quoting differences, using only the last relation-name
part even across schemas. Every synthetic aggregate except `sum`, `min` or `max` of
one bare column also shares a relation-row dependency with governed count measures,
including normalized `entity_count` measures, because any other form can recover a
row count. `aggregate_if` conditions have no authored field and cannot satisfy
`allowed_where`; other unrelated source columns remain independent.
Column binding and caveat temporal-shape inspection retain their specialized views.

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
7. Execute the prepared SQL against the configured warehouse.

Important planner behaviors:

- safe mixed-grain cases compile via leaf pre-aggregation rewrites
- exact aggregate relations can be selected for compatible time-grain measure
  leaves; routed leaves expose `aggregate_relation_id` and physical/performance
  plan metadata
- historical joins use temporal-validity conditions anchored to the effective time axis
- a key dimension reached by a hop into the validity window uses that temporal
  join and validity rewrite, even when the source has a matching foreign-key column.
  Missing history versions group under NULL; without a query time, incoming history
  lookups refuse with `FANOUT_UNSAFE`. Co-located keys keep their source-column shortcut
  for non-temporal hops and hops out of the table holding the window, where each source
  row is already one version. Jaffle Shop labels its historical customer key distinctly
  so all-time customer rankings use the ordinary customer key
- a many-to-one or one-to-one hop never removes a measure's row: it is a left join in every
  leaf, whatever reads the looked-up dimension, so a row with a NULL or unmatched foreign key
  keeps its measure value under NULL. Only these reads keep an inner join: a time role
  (`_INNER_LOOKUP_PURPOSES`), a metric predicate's route to its entity and its own nested
  query, a distribution's per-entity values (both `inner_lookups`), conversions, the hops
  from an `entity_in_terms_of` anchor back to the counted entity, a dimension any rollup of
  the measure's model holds (even at a grain that rollup can never answer; the
  `entity_in_terms_of` leaf leaves such a query to the measure's own leaf, and
  `_joins_for_paths` refuses it from another model's rows with `REWRITE_NOT_SUPPORTED`; a
  rollup of any other model, and any rollup in a query of dimensions alone, changes no
  join), and every hop on
  a dialect without `outer_lookup_joins` (ClickHouse, whose unmatched outer-join columns read
  a type default, not NULL); a hop any of them walks is inner for every read. Hops that fan
  out are inner joins. `_joins_for_paths` (`compiler_parts/paths.py`) is the one place that
  decides, and the only caller of the join-condition builder; the `parent_lookup` leaf
  described below is its named exception
- an `entity_in_terms_of` count always requires a matching row of the counted entity,
  including when grouping only by the child's lookup without a parent dimension or time
  axis. The shortcut requires exactly one relationship between child and parent, on the
  counted path, with an available forward lookup; otherwise the measure's own leaf answers.
  Its anchor plan adds that same relationship; `_joins_for_paths` checks both the relationship
  and emitted joins, and refuses a missing, nullable or different parent check with
  `REWRITE_NOT_SUPPORTED` if the shortcut's eligibility check is bypassed
- dense fill uses the declared calendar entity for the requested calendar id, or the implicit
  Gregorian calendar for a default request in a package that declares no default calendar
- `metric_predicate` compiles as a scoped predicate subplan rather than a projected boolean expression
- query-time predicates default to contextual scope
- package-authored predicates must declare `scope_mode`
- supported v1 predicate modes are `contextual` and `entity_only`
- contextual predicates inherit outer time and compatible grouped context entities, and inherit compatible filters without widening the join key; a dimension on the input's own row entity contributes its values, joined null-safely, instead of that entity's key
- distribution branches refuse post-aggregation metric filters rather than evaluating them at the per-entity grain; contextual predicates on a different entity also refuse because branch grouping cannot preserve the outer context, including predicates bound inside the input through scoped aggregates or metric recipes
- contextual `time_grain` overrides are limited to coarser deterministic ancestor buckets on the same calendar
- entity-only window predicates use the compatible query clock or a sole compatible input clock; an incompatible query clock with multiple input clocks refuses centrally in `_predicate_time_spec` with `INVALID_TEMPORAL_BINDING`, including during direct SQL lowering
- supported conversion requests compile as event-pair matching subplans
- unsupported conversion requests fail semantically rather than silently degrading into ratios

The `parent_lookup` leaf compiles its source measure as an inner query grouped by the
single-column `via` key under the same bindings, including access policies, and records
the child's direct relationship in the owning leaf's dependencies. It applies the source's
settlement gate, LEFT JOINs the settled source onto the child's direct foreign key, and
uses MAX over values proved single per output row. `_validate_non_additive_sums`, through
`_single_valued_columns`, is the one enforcement point for that proof: planning and the leaf
both call it, and it refuses a different resolved route or unsafe grain with `ROLLUP_UNSAFE`.
The loader refuses composite `via` keys and recorded routes that conflict with either
direct relationship. This leaf's explicit LEFT JOIN to a compiled total is the named
exception to the `_joins_for_paths` rule above. Its CTE allocator skips a source name
when any occupied relation or CTE name contains it, ignoring case, so gate and nested
names remain distinct from physical relations after namespacing.

Stocks require snapshot selection before attribute filters. Planning refuses stock
fan-out paths, and package loading refuses a stock lookup source. `lower_to_sql` centrally
refuses a supplied `fanout_dedup` plan over a stock (including its semi-join leaf), and a
supplied `parent_lookup` plan whose source is a stock, with `REWRITE_NOT_SUPPORTED`, reason
`stock_requires_snapshot_selection`. The refusal names the stock source and, for a parent
lookup, its consumer, including with empty-group guards off.

### Loaded semantics and executable SQL

`load_package_snapshot(path)` captures a stable source inventory and parses it
once into a `LoadedPackageSnapshot`. Its authored, normalized, typed config, and
canonical semantic views come from those captured bytes. Each public view is an
isolated copy. `Runtime.from_snapshot(snapshot)` reuses that generation; runtime
requests, catalog manifests, package comparisons, and semantic exports bind to its
identity. Reload replaces the complete generation under the existing state gate.
Editing `runtime.config` changes only the returned copy: use `Runtime.from_config`
for an explicit replacement or `reload()` to load edited sources. Engine internals
use the private owned config, avoiding repeated copies on query paths.

The framed source fingerprint includes relative source paths and bytes. The
semantic fingerprint identifies the canonical parsed semantics, excluding
connection settings, seed configuration, and local database paths. Entity relations
and warehouse semantics remain significant. In-memory configs have their own
identity that preserves exact typed-list ordering and make no claim to match files
on disk. When several entities share a physical table, a measure binds its own
entity; other table-only references require an explicit entity instead of choosing
whichever entry appeared last. Compiled manifests include both
identities and source provenance; stale artifacts fall back to the loaded runtime.
Package checks reject edits during validation, and artifact builds verify captured
source bytes against the validated manifest before writing them.

The compiler's final dialect preparation creates a frozen `PreparedQuery` containing
executable SQL and physical-to-semantic column mappings. Compile and explain expose
that SQL; built-in adapters execute it unchanged through `query_prepared`, restoring
semantic aliases in result rows. Session timeout commands and row-fetch limits remain
adapter concerns. Legacy direct `query(sql)` calls use the same preparation once.
A statement may carry typed parameter slots that the runtime binds per request
from trusted attributes; only adapters that bind values separately execute it
(see [ADDING_A_DIALECT.md](ADDING_A_DIALECT.md)). Row-filter policies produce
them: after lowering, `semantic_rails.row_filters` adds `<column> = ?` to the one
ordinary scan and every engine-tagged observation or coverage scan of a filtered relation.
It denies other repeated reads, joins, other relations and rollups (routing is off under
a row filter). DuckDB binds `?` directly; Postgres preparation finalizes slots as
`$1`, `$2`, … and ADBC validates them before connecting. Other adapters deny
parameterized SQL. Empty-group settlement lives in `compiler_parts/empty_groups.py`: untimed
observation determines whether zero is defined, each sum's leaf counts the rows it read so
zero goes only to a group with none (never to rows whose amounts are all NULL; a leaf without
the count is refused). A query with a distribution branch, and a metric predicate's source
over several measures under a threshold 0 passes, keep the earlier settlement
(`earlier_settlement` in `compiler_parts/bind.py`) and plan and lower exactly as before. A
measure whose expression contains a CASE below its root also keeps the earlier settlement
individually and refuses rollups; lowering refuses a plan that bypasses that routing rule.
Each row count is named `m<i>_rows` beside its measure's `m<i>`, renamed until no output, key or
package column has the name, so the alias registry never rewrites it. Source rollups also
rename their internal value until it differs from every projected join column, group, time
and row-count alias, using case-insensitive comparison. Base time coverage
bounds only zero
substitution on filled, dense, combined or retained filtered leaves. One predicate decides
both coverage and rollup refusal, on DuckDB and Postgres only. Populated values pass through;
routed queries keep the window test and never scan a shadow raw leaf.
On those warehouses, plain filtered additive leaves on a local clock retain source buckets by
evaluating authored dimension filters in conditional operands. One rule names the retained
leaves for lowering and the guard, and a plan with one emits coverage, filled or not. The
settlement guard recognizes a retained leaf's row count only where coverage gates its
bucket, so a no-match bucket reads zero inside the loaded range, NULL after it, and matching
unknown amounts stay NULL; folding keeps leaves with the same retention semantics together.
Lowering refuses a retained-bucket claim without its conditional operand. Unsupported
filtered series, and every filtered series elsewhere, keep their existing SQL and diagnose
dropped buckets through a bounded, separately authorized runtime query.
Segment preview and count execute their prepared statements independently; the
preview response includes both statements. Live valid-values uses the ordinary
query path and includes the loaded semantic identity in its provenance.

### Compile Cache Seam

The runtime memoizes compiled plans behind a cache keyed on a sha256 of the
normalized query, package fingerprint, warehouse, render profile, policy
context, trusted attribute values, and aggregate-routing switch
(`semantic_rails.cache.compilation_cache_key`). Requests whose attribute values
differ never share a cached plan.
The default backend is
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

- DuckDB is the zero-setup local backend; Snowflake execution depends on a configured `snowflake_cli`, `snowflake_native`, or opt-in `snowflake_adbc` connection (see [ADBC profiles](ADDING_A_DIALECT.md#adbc-profiles)).
- The executed conversion family is intentionally scoped to the supported event-count model rather than a fully general conversion planner. Each operand counts the rows of its entity's table by the entity key, so an operand measure must count exactly that key, spelled as the entity declares it: a measure that counts an expression (such as `CASE WHEN ... THEN key END`), another column or a fact model's rows is rejected with `CONVERSION_NOT_SUPPORTED`. Write a `CASE` condition as the operand's `filter` instead, and for another column use a measure on the entity whose rows are the events. Both operands counting the conversion entity itself on one clock is rejected too, by package validation and at query time: each entity is then a single event that converts to itself, so the window never applies.
- `metric_predicate` is implemented for the supported contextual and entity-only cases used by the active package, but it is not yet a fully general arbitrary nested predicate planner.
