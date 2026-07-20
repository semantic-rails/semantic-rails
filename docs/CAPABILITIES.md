# Capabilities

The active executed fixture in this repo is [configs/semantic_rails/jaffle_shop](../configs/semantic_rails/jaffle_shop). This guide describes the current runtime surface, including the schema and the broad executed Jaffle fixture package.

The public runtime is the `semantic_rails` package and its documented API
surface.

## Strongly Supported

### Package Authoring

- `schema_version: 1` ergonomic directory packages
- top-level `package.yml`, `graph.yml`, optional `defaults.yml`, `policies.yml`, and `caveats.yml`
- package-local `examples/` and `tests/`
- recursive loading from `models/**` and `metrics/**`
- graph-first entity identity with ordered compound keys
- model-centric authoring by mart or table
- explicit measure shape via `kind` / `accumulation` / `value_type` (no shorthand `primitive:`)
- implicit FK-to-entity-key joins for standard cases
- explicit joins for temporal validity and other non-default cases
- `name` and `label` on public objects
- `model.variants:` authoring for exact physical rollups at alternate time
  grains, normalized by the loader into aggregate relations

### Querying

- measure references
- metric references
- group-by-only distinct-values queries
- time-only distinct-values queries with an explicit temporal role
- arithmetic expressions
- cumulative expressions
- rolling expressions
- prior-period expressions
- period-to-date expressions
- post-aggregation metric filters
- query-level time grain selection
- `time.fill`
- `time.calendar_id`
- compile and validate before execution
- `order_by.field = "time"` when a query has a time axis

### Planner Semantics

- safe fact-to-dimension traversal
- multi-hop entity traversal with per-hop cardinality checks (default ceiling
  4 relationships; raisable to 8 via `graph.path_policy.max_hops`)
- ambiguous-path rejection
- route pinning via `graph.path_preferences` (load-time validated)
- unpinned-alternate-route warning (`PATH_ALTERNATES_UNPINNED`) and
  conflicting-route refusal (`PATH_JOIN_CONFLICT`)
- per-query `hop_profile` reporting (chosen chains, per-hop safety,
  long-hop targets)
- explicit root selection for no-measure distinct-values queries
- unsafe-fanout rejection
- supported mixed-grain rewrites through independent leaf aggregates
- exact aggregate-relation routing for compatible time-grain measure leaves
- temporal-role compatibility enforcement
- temporal-validity joins for historical attributes
- first-class `metric_predicate` support for supported contextual and entity-only cases
- true event-pair conversion execution for the supported event-count model
- explicit `CONVERSION_NOT_SUPPORTED` rejection for enriched conversion shapes outside the supported execution model

### Metric Families Demonstrated In The Active Jaffle Package

Each family below is exercised by at least one metric in
`configs/semantic_rails/jaffle_shop/metrics/`. Where two patterns are valid
(direct query-time computation vs. semi-additive over a precomputed rollup
measure), jaffle ships both so authors can compare the trade-offs.

- simple aggregate metrics (`kind: aggregate`)
- filtered metrics (filter expressions inside aggregate metrics)
- ratio metrics (`kind: ratio`)
- derived metrics — direct authoring with `kind: derived`
  (e.g. `metric.sales.gross_margin_pct` in `metrics/extensions/derived_metrics.yml`)
- cumulative metrics (`kind: cumulative`, e.g. `metric.sales.cumulative_revenue`)
- rolling metrics — both direct query-time `kind: rolling`
  (`metric.sales.rolling_7d_revenue_direct`) and the semi-additive-over-precomputed
  pattern (`metric.sales.rolling_7d_revenue`)
- prior-period metrics — direct `kind: prior_period`
  (`metric.sales.prior_week_revenue_direct`) and semi-additive proxy
  (`metric.sales.prior_period_revenue`)
- period-to-date metrics — direct `kind: period_to_date`
  (`metric.sales.revenue_mtd_direct`) and semi-additive proxy
  (`metric.sales.revenue_mtd`)
- snapshot-like start-of-period and end-of-period metrics (`kind: semi_additive`
  with `accumulation: { kind: stock, snapshot: ... }`)
- historical attribute slicing
- executed event-pair conversion metrics
- alternate-calendar dense fill
- median and percentile aggregations

### Metadata APIs

- catalog browsing with four verbosity tiers (`summary`, `minimal`, `compact`, `full`)
- standalone compact `capabilities` MCP tool (loop position 0 orientation)
- guided discovery via `discover`, `inspect`, `build-options`, and `plan`
- stage-aware discovery and builder ranking
- resolution by canonical ID, `name`, `label`, and inline synonyms
- `build-options` as the preferred guided-builder API
- step-oriented `build-options` responses with `recommended`, `available`, `blocked`, and `query_patches`
- value-domain and query-backed `valid-values`
- structured validation diagnostics
- explain artifacts with logical plan and SQL AST
- compact history/null-preserving coverage warnings on inspect, validate, compile, and query when temporal-validity joins are active
- capability flags and unsupported-capability reasons
- valid and disabled grouping entities
- metric temporal-role metadata
- dimension provenance for non-local dimensions
- measure-first aggregation guidance, recommended dimensions, and search-term ranking
- plan output with `status`, `intent_ir`, `best.query_ir`, and optional alternatives
- comparison-aware planning with validated inline comparison drafts
- comparison-family and clock-variant metadata on object cards
- starter query patches on measure, metric, and dimension inspection cards

### Relation-Pipeline Primitives (EXPERIMENTAL — not part of canonical V1)

> **EXPERIMENTAL.** Semantic Rails is not an ELT tool, orchestrator, CTE
> pipeline compiler, or materialization service. The broader 12-step
> `relations:` pipeline DSL was pruned before launch. The surface below is the
> *limited* relation contract that planning already uses — it is not a
> user-facing pipeline API and may be removed or renamed before V2.

The shipped runtime does not expose a general relation-pipeline API. The
relation surface is just the set of primitives metric planning relies on
internally:

- package-authored model `relation` references
- graph entities with declared grains and keys
- semantic join contracts and temporal-validity join metadata
- segment definitions that compile to preview and membership queries
- logical-plan metadata, including selected relation information
- typed SQL AST lowering and rendered warehouse SQL
- package-local examples, tests, artifacts, and impact reports

Do not imply that arbitrary raw SQL, multi-step CTE chains, user-authored
materializations, or warehouse transformation orchestration are shipped
runtime capabilities.

## Supported With Guardrails

### Mixed-Grain Planning

The runtime supports planner-owned rewrites for supported mixed-grain cases by compiling leaf aggregates separately and joining them on the final grain.

In practice:

- safe direct paths compile normally
- some mixed-grain measure combinations compile via leaf pre-aggregation rewrite
- unsupported grain-expanding shapes still fail fast rather than silently miscomputing

Relevant statuses and codes:

- `rewrite_strategy.status = "direct"`
- `rewrite_strategy.status = "rewritten"`
- `MIXED_GRAIN_INVALID`
- `REWRITE_NOT_SUPPORTED`

### Physical Variant Routing

The runtime supports conservative routing from a semantic model to exact
package-authored physical rollups. Authors define one model with
`model.variants:` for transaction, day, week, month, or other stored grains.
The loader normalizes non-transaction variants into aggregate relations, and
the compiler selects a rollup only when it can prove the requested measure,
time grain, grouped dimensions, and filters are covered.

Current guardrails:

- only `source: default` is supported; different warehouse/source routing is
  intentionally future work
- `equivalence.kind` must be `exact`
- routed measures must be additive or precomputed over the selected rollup
- grouped and filtered dimensions must exist on the variant
- query-time `metric_predicate` filters do not route through variants yet

Surface signals:

- `logical_plan.measure_plans[].aggregate_relation_id`
- `physical_plan.nodes[].details.selected_relation_type`
- `physical_plan.nodes[].details.aggregate_relation_id`
- `performance_plan.aggregate_routing.selected`

### Metric Predicates

`metric_predicate` is supported where the predicate input can be evaluated safely at the declared entity and reconciled to the outer query context.

Current guardrails:

- predicate expressions are valid inside published metrics and `metric_filters.expression`
- predicate expressions are not selectable directly as projected result columns
- query-time predicates default to contextual scope
- package-authored predicates must declare `scope_mode`
- contextual predicates inherit the outer time bucket and compatible grouped context entities
- outer compatible filters are inherited into the predicate subquery without widening the scoped join key
- finer-grain or non-deterministic time mixes fail with semantic errors rather than smearing values

#### Inline percentile-as-threshold

`metric_predicate.value` accepts either a literal scalar or an inline
percentile expression `{kind: "percentile", p: <float in [0,1]>,
measure?: <id>}`. When the value is the percentile shape the compiler
materialises a one-row threshold CTE alongside the predicate-source
CTE and CROSS JOINs it, so a single query both computes the threshold
and applies it — eliminating the most common two-round-trip pattern
("compute P90, then filter ≥ P90"). The optional `measure` key must
match the predicate's input measure; cross-measure thresholds remain
a separate two-step pattern. Worked example:
[`examples/percentile_threshold.json`](../examples/percentile_threshold.json).

#### Plan phrase support for population-percentile rollups

The plan pattern detector recognises natural-language phrases
naming a population percentile and emits the inline-threshold IR
directly. Triggers include:

- `top decile of` → `p = 0.9`
- `top quintile of` → `p = 0.8`
- `top quartile of` / `top quarter of` → `p = 0.75`
- `top third of` → `p ≈ 0.667`
- `top half of` → `p = 0.5`
- `top N% of` / `top N percent of` → `p = (100 - N) / 100`
- `above the Nth percentile` → `p = N / 100`

Example: `"revenue from top decile of customers by lifetime spend"`
produces a `scoped_aggregate` IR with a `metric_predicate` whose
`value` is `{kind: "percentile", p: 0.9}`. The detector reuses the
existing `_qualified_metric_rollup` path — no new pattern, only new
trigger phrases and percentile-shaped threshold values.

### Output Safety

- duplicate projected output names are rejected with `DUPLICATE_OUTPUT_ALIAS`
- collisions are checked across explicit select aliases, grouped dimension IDs, and the projected time-axis alias
- cumulative expressions with a bounded `query.time.start` fail with `CUMULATIVE_TIME_FILTER_UNSUPPORTED` instead of silently computing from truncated history

### Historical Joins

Historical slicing works when the relationship declares `temporal_validity` and the requested time anchor can be reconciled safely.

Current behavior:

- missing history is null-preserving
- historical joins become left-join style paths once validity windows are applied
- the API now emits compact warnings/caveats so a `NULL` bucket can be interpreted as “no valid history row at the time anchor”
- unsupported historical shapes fail semantically rather than silently dropping rows

## Current Limits

### Execution Backends

- DuckDB is the default local execution engine and supports package-local seed/bootstrap workflows.
- Snowflake SQL rendering is supported through the Snowflake dialect. Live execution is available through the Snow CLI adapter when `package.connection.kind: snowflake_cli` is configured, or through the optional native connector when `package.connection.kind: snowflake_native` and `semantic-rails[snowflake]` are installed.
- Managed cloud execution-on-behalf is intentionally not part of the local runtime contract; use `compile` for SQL/plan output and customer-side connectors for execution.

### Package Verification And Artifacts

- `semantic-rails check` runs parse, validation probes, package-local examples, and package-local tests as one GitHub-friendly gate.
- `semantic-rails build-package` and `semantic-rails check --artifact` write a manifest-backed `.tar.gz` package artifact containing the config source files and package hash.
- Package-local `examples/` and `tests/` now provide the customer-owned regression surface for deployable configs.
- The active Jaffle package has a release authoring profile: every public measure and curated metric declares owner/review/risk metadata plus package-local test links. `check` passes with advisory authoring warnings for intentional label and search-term overlaps.

### Conversion Metrics

- The enriched event-pair conversion shape is part of the expression surface.
- The runtime executes the supported event-count conversion model with entity, window, matching mode, and optional constant-property matching.
- Unsupported conversion shapes still reject explicitly with `CONVERSION_NOT_SUPPORTED`.

### Predicate Generality

- `metric_predicate` is implemented for the supported contextual and entity-only cases used by the active package.
- It is not yet a fully general arbitrary nested predicate planner.

### Path Ranking

- Path selection is governed and deterministic.
- Path ranking is still simpler than a fully costed semantic planner.

### Benchmarks and Operations

- The repo has validators, smoke checks, and measured plan benchmark scorecards. See [BENCHMARK_EVIDENCE.md](BENCHMARK_EVIDENCE.md) for the release evidence commands.
- It ships Docker/ASGI, health/readiness, MCP transports, optional API-key checks, and JSON audit events, but does not ship a managed production observability control plane.

### Relation Pipeline Generality (EXPERIMENTAL boundary)

A generalized relation pipeline (arbitrary derived relations, chained CTEs,
materialization policies, warehouse-managed transforms) is **not** in the V1
contract and is not on the roadmap. Semantic Rails is a semantic layer, not
an ELT tool. Raw analysis SQL can be migrated into governed package assets
only when it maps to existing model, join, measure, metric, segment,
predicate, or conversion primitives. Anything else stays as workaround SQL or
an explicit hard failure rather than being counted as a shipped capability.

## Cross-clock Queries

A query is "cross-clock" when it touches more than one declared
temporal role — for example a session-anchored conversion that the
caller also wants to filter by an order-anchored time bucket. The
layer's behavior is deterministic but the supported shapes are
narrow: read this section before composing one.

### Pairings that work natively (same anchor)

If every metric/measure in `select` shares one temporal role and the
top-level `time.temporal_role` is that same role, the query compiles
and executes without any conversion semantics. This includes the
positive same-store 7d conversion case anchored on
`temporal_role.jaffle_session_started_at`:

```jsonc
{
  "select": [
    {
      "expression": {
        "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
      },
      "as": "same_store_conversion_rate_7d"
    }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_session_started_at",
    "grain": "day"
  }
}
```

Worked file: [`examples/cross_clock_conversion.json`](../examples/cross_clock_conversion.json).

### Pairings that require an authored conversion metric

Cross-clock semantics — for example, "filter sessions, count orders" —
must be packaged inside an authored conversion metric such as
`metric.sales.session_to_order_conversion_rate_7d`. The metric
declares the base clock, the converted clock, the entity matching the
two events, and the window. The runtime then executes a governed
event-pair conversion. Free-form cross-clock joins in user-supplied
IR are not supported; reach for an authored metric instead.

### Pairings that fail at validate with `INVALID_TEMPORAL_BINDING`

If the top-level `time.temporal_role` does not match the conversion
metric's declared anchor (and is not the converted clock either), the
runtime now rejects the request at `validate` with the
`INVALID_TEMPORAL_BINDING` error code (added in the Phase 3 binder
safety gate). Before that gate, the same query would compile and
fail at execute with a DuckDB binder error. The error envelope's
`recovery_hints` includes the metric's allowed anchor roles and a
suggested rewrite (`kind: "filter_on_conversion_anchor"`).

```jsonc
{
  "select": [
    {
      "expression": {
        "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
      },
      "as": "rate"
    }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month"
  }
}
// -> validate.ok = false
// -> errors[0].code = "INVALID_TEMPORAL_BINDING"
// -> errors[0].details.anchor_temporal_role
//      = "temporal_role.jaffle_session_started_at"
// -> errors[0].recovery_hints[0].kind
//      = "filter_on_conversion_anchor"
```

## What This Layer Does NOT Do Yet

These are deliberate, documented gaps. They are tracked under the
`// TODO(point-in-time-joins)` marker (and similar) so future work
lands in one place rather than scattering across the codebase.

- **Point-in-time dimension joins ("as-of" snapshots).** The runtime
  cannot pin a slowly-changing dimension to its value as of an
  arbitrary anchor time in the query (for example, "store name as it
  was at month-start"). Historical attribute slicing works only when
  the relationship declares `temporal_validity` and the anchor is
  reconciled by the planner; arbitrary as-of pins are not supported.
  See `// TODO(point-in-time-joins)` in
  `semantic_rails/compiler_parts/temporal.py`.
- **Per-row event-anchored windows.** Aggregations over a window
  that varies per row of an outer entity (e.g. "revenue in the 90
  days following each customer's first order") are still not
  executable end-to-end. The IR contract DID land in round three —
  `scoped_aggregate` accepts `anchor: {temporal_role}` and
  `window: {unit, value, direction}` keys, the parser validates
  the shape, and the bind layer raises `INVALID_ANCHOR_ROLE` with
  `feature_status: ir_contract_only` and a structured
  `feature_pending_sql_lowering` recovery hint when the SQL
  lowering would be invoked. Authors can build queries against
  the locked IR shape today; SQL emission ships in the next
  round. Supported shapes meanwhile: a pre-authored windowed
  measure inside a metric recipe, or a query-time
  rolling/prior-period/period-to-date primitive over a *uniform*
  clock.
- **Cross-clock filters without an authored conversion metric.**
  Free-form "filter on clock A, aggregate on clock B" queries are
  rejected with `INVALID_TEMPORAL_BINDING` (see "Cross-clock queries"
  above). The supported shape is to publish a conversion metric.
- **Arbitrary chained derived relations / user-authored CTE chains.**
  Semantic Rails is not an ELT tool — see the experimental
  relation-pipeline boundary in this document.
- **Fully general nested predicate planning** beyond contextual and
  entity-only cases used by the active package. See "Predicate
  Generality" below.
- **Runtime JSON-Schema enforcement** of the published
  [`schemas/query_ir.v1.json`](../schemas/query_ir.v1.json). The
  schema is the durable contract today but the runtime does not yet
  reject unknown top-level keys against it (forward-compat); the
  parser raises `INVALID_EXPRESSION_KEY` only inside expression
  positions.

## When To Expect An Error

You should expect a hard failure when a request is ambiguous or unsafe, for example:

- alias or human-facing token matches multiple semantic objects
- two equally valid paths exist and no deterministic preference breaks the tie
- a target entity is reachable only beyond the configured hop ceiling
  (`PATH_NOT_FOUND` with `details.reason: hop_limit_exceeded`)
- one query needs the same table through two different relationships
  (`PATH_JOIN_CONFLICT`)
- an aggregation is not allowed for the selected measure
- a temporal role is invalid for the selected measure
- a fanout is unsafe
- a mixed-grain rewrite is not supported for the query shape
- a predicate grain would smear values across the outer query
- a conversion request falls outside the supported event-count execution model
- no valid-values source exists for the requested dimension
- a request requires arbitrary chained SQL, many-to-many expansion, or a
  materialization boundary the package contract has not modeled (see the
  experimental relation-pipeline boundary above)

## Active Jaffle Coverage

Good for:

- retail-style revenue and order metrics
- store, product, customer, and supply slicing
- multi-fact order and item queries
- guided, API-only query building from business terms like `orders`, `drinks`, or `average order value`
- side-by-side planning for common comparison asks like food vs drink
- planner-backed coordinated comparison bundles for cross-clock asks like ordered vs delivered revenue
- executed session-to-order conversion metrics, including same-store matching
- dense time-series analysis
- rolling, PTD, and prior-period examples
- compound-key snapshot entities
- historical customer attributes
- multiple business clocks
- fiscal-calendar examples
- governed predicate-style metrics such as repeat/high-value customer orders and contextual “10+ orders in period” metrics
- package-local regression coverage for core finance, customer lifetime, product margin, supply, time-series, monthly comparison, snapshot, lifecycle, predicate, distribution, and conversion/adoption primitives

Not yet complete for:

- a broader fully general predicate planner beyond the supported contextual and entity-only cases
- a richer managed execution story beyond local/customer-side DuckDB and Snowflake connector workflows

## Worked-example IR files

Each shape called out above has a committed IR file under
[`examples/`](../examples). The regression suite at
[`tests/semantic_rails/test_examples.py`](../tests/semantic_rails/test_examples.py)
runs every file through schema validation, `Runtime.validate`, and
`Runtime.compile` so the docs stay honest as the IR contract evolves.

| Shape | File |
| --- | --- |
| Top-N rollup against a single measure | [`examples/jaffle_shop_revenue_by_store.json`](../examples/jaffle_shop_revenue_by_store.json) |
| Inline YoY (`kind: prior_period` in `select`) | [`examples/inline_yoy.json`](../examples/inline_yoy.json) |
| Same-store 7d conversion with anchor-aligned time block | [`examples/cross_clock_conversion.json`](../examples/cross_clock_conversion.json) |
| Qualified-metric rollup ("top X with at least N") | [`examples/qualified_metric_rollup.json`](../examples/qualified_metric_rollup.json) |
| Inline percentile-as-threshold ("top decile by lifetime spend") | [`examples/percentile_threshold.json`](../examples/percentile_threshold.json) |

## Recommended User Path

1. Start with [README.md](../README.md) for install and first query.
2. Use [PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md) to model or edit a package.
3. Use [AGENT_QUICKSTART.md](AGENT_QUICKSTART.md) when wiring an agent or MCP client.
4. Use [QUERY_API.md](QUERY_API.md) for request and response shapes.
   Use the preferred cascade: `discover -> inspect -> plan/build-options -> valid-values -> validate -> compile -> execute`.
5. Scaffold a new package with `semantic-rails init` (it validates as it generates). [configs/examples/semantic_rails_package_starter.yml](../configs/examples/semantic_rails_package_starter.yml) is a *single-file* package starter — if you explode it into a directory package, drop the `grain:` keys (directory packages reject them under `schema_strict`).
6. Use [ARCHITECTURE.md](ARCHITECTURE.md) when you need the architecture spec rather than the user guide.
