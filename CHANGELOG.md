# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- A packaged, versioned public contract bundle for package authoring, stable and
  preview Query IR, HTTP, query MCP, framework-neutral semantic validation, and
  validation-report envelopes. Deterministic generation, compatibility checks,
  and wheel/sdist verification make these artifacts release gates.
- A stable `semantic_rails.contracts` producer for dbt, SQLMesh, and future
  validation bindings, plus the `semantic_rails.embedding` facade and a generic
  in-memory warehouse credential-provider seam for independent engine hosts.

### Changed

- Query MCP tool discovery now publishes output schemas and standard behavioral
  annotations. Stable Query IR v1 accepts only `version: 1`; version 2 has a
  separately named preview schema.
- Architect MCP mutations now require optimistic project revisions and
  idempotency keys, serialize across processes, parse-gate atomic multi-file
  commits, roll back failures, and support exact write-free previews through a
  versioned generated interface contract.
- The public repository and distributions now contain only the standalone
  engine. Site deployment code and service-client commands are maintained
  outside this release boundary; local profiles select package paths only.
- Contract schema identifiers use the controlled
  `https://semantic-rails.com/schemas/` mirror. PyPI-verified wheel/sdist bytes,
  every contract JSON artifact, and `SHA256SUMS` are attached to the GitHub
  Release without a second build.

## 0.2.0 — 2026-07-09 — Governed onboarding and release hardening

### Added

- Guided local onboarding with `setup --interactive`, reusable local profiles,
  managed MCP `start` / `stop` / `status`, and Claude/Codex client-config generation.
- Architect MCP workflows for scaffolding, inspecting, and validating Semantic Rails
  packages without weakening the deterministic CLI baseline.
- Precomputed catalog manifests shared by HTTP, MCP tools, and MCP resources, with a
  context-safe live-computation fallback.

### Changed

- Remote HTTP and MCP requests now resolve one trusted request context. Caller-supplied
  policy context cannot override authenticated tenant, role, audience, environment, or
  project fields, including on segment workflows.
- Plain grouped questions such as “orders by store” no longer invent a monthly grain;
  unresolved or low-confidence plans are never marked ready for execution.
- ASGI work runs through a bounded queue while health checks remain responsive. Shared
  warehouse connections are serialized, cursor reads are bounded, truncation is explicit,
  and DuckDB timeouts use an interrupt watchdog.
- Release, package-distribution, and dependency-audit gates now verify the exact
  artifacts and semantics they publish.

### Security

- Policy-sensitive catalogs are no longer shared through the edge cache.
- Managed MCP processes verify OS-observed process identity before signaling a stored PID.

## 0.1.1 — 2026-06-24 — Warehouse expansion and release polish

### Added

- **Warehouse-dialect expansion: nine registered warehouses.** Postgres, BigQuery,
  Databricks, Athena, ClickHouse, MotherDuck, and DuckLake join DuckDB and Snowflake as
  first-class execution targets, each as one dialect class + one adapter + one registry
  entry (`semantic_rails/dialects.py`); option validation, secret resolution
  (env-var/file indirection only — literal credentials are rejected at parse time),
  redacted error envelopes, and factory dispatch are shared machinery
  (`semantic_rails/db_parts/common.py`). Dialect quirks are reconciled to exact DuckDB
  parity — exact percentiles rebuilt from sorted `ARRAY_AGG` where the warehouse only
  offers approximate sketches (BigQuery, Athena), boundary-crossing `date_diff` and
  clamped calendar `date_add` relowered on Postgres, FULL-JOIN-compatible null-safe
  equality on Postgres/BigQuery, and an exact `LAG` emulation on ClickHouse.
  Documented, literal-aware compat passes rewrite rendered SQL only where a hard
  warehouse limit demands it (identifier shortening and float ratio division on
  Postgres; backtick quoting and field-name legalization on BigQuery/Databricks;
  typed temporal literals on Athena).
- Optional pip extras per driver: `semantic-rails[postgres|bigquery|databricks|athena|clickhouse]`
  (joining `snowflake` and `server`), plus `all` for every connector. MotherDuck and
  DuckLake reuse the core `duckdb` dependency. A missing driver maps to a structured
  `MISSING_DEPENDENCY` error naming the extra.
- Cross-warehouse conformance suite (`tests/integration/`): every registered warehouse
  must return row-for-row identical results to the DuckDB reference for the full query
  battery (worked examples + the jaffle_shop package's own test queries) over an
  identical fixture. Targets without credentials skip (`SR_INTEGRATION_STRICT=1` for CI
  posture); a registry-coverage test fails any warehouse registered without a
  conformance target. `make warehouses-up` provisions local Postgres + ClickHouse via
  docker compose; `make test-integration` runs the suite.
- `docs/ADDING_A_DIALECT.md` — the add-a-warehouse guide, with Redshift as the worked
  example (Redshift ships as a documented stub: env-var names reserved in
  `.env.example`, registry block ready to uncomment, account pending verification).

## 0.1.0 — 2026-06-11 — Initial public release

Semantic Rails ships as an Apache-2.0-licensed agent runtime for governed metrics, structured around a
deterministic `discover → inspect → plan/build-options → valid-values → validate → compile →
execute` loop served over MCP (stdio + HTTP) and a 16-route `/api/v1/*` surface. This is the
first published release; everything in this entry (and the feature inventory below) is part
of it.

### Added

- Package-authored semantic caveats: an optional `caveats.yml` (or inline
  `semantic_caveats:` block for single-file packages) attaches advisory
  interpretation context — business events, definition changes, data-quality
  notes — to semantic objects, entity values, and time windows. Matching
  caveats surface as `SEMANTIC_CAVEAT_APPLIED` warnings on `validate`,
  `compile`, and `query`/`execute` (with `SEMANTIC_CAVEATS_TRUNCATED` past the
  verbosity cap); they never alter SQL, rows, access, discovery, or policy
  behavior. Caveats are gated by the same audience/environment scoping as
  policies, validated at load time against declared object ids, counted in
  the package manifest summary, and tracked by `diff-package` /
  `impact-report` as metadata-only changes.
- Entity hopping is now policy-controlled and observable. `graph.path_policy.max_hops`
  (default 4, max 8) replaces the hardcoded hop ceiling; `graph.path_preferences`
  pins the join route per entity pair and is validated at load time. Queries that
  need the same table through two different relationships refuse with
  `PATH_JOIN_CONFLICT`; routes chosen by hop count alone with an undeclared
  alternate emit a `PATH_ALTERNATES_UNPINNED` warning; `PATH_NOT_FOUND` now
  distinguishes `hop_limit_exceeded` from `no_relationship_chain`. Every
  compile/query response carries a `hop_profile` (chosen chains, per-hop
  cardinality/safety, long-hop targets) for acceleration-layer telemetry.

### Fixed

- **Authoring mistakes that every validator silently accepted now fail loudly with located,
  actionable errors.** A blind-author evaluation planted realistic mistakes in a fresh `init`
  package and found a class of shape errors that passed `validate-config`, `check`, and `doctor`
  unflagged:
  - Unknown keys in package/graph/model/dimension/time/measure/metric/segment/join blocks
    (e.g. `agg:` for `default_agg:`) were silently ignored; they are now rejected with
    did-you-mean hints or the full allowed-key list. The document top level flags only
    close-match typos (`modles:` → `models`), so annotation blocks like the capabilities
    reference remain valid.
  - A scalar `domain:` (e.g. `domain: new`) was iterated character-wise into the value set
    `['n', 'e', 'w']`; non-list `domain`/`valid_values`/`supported_grains`/`environments` are
    now rejected with a written-out fix.
  - The dimension-kind/time-kind/time-class enum checks only ran for directory packages;
    single-file packages (`init`'s output) now run them too, plus new measure-kind,
    metric-kind, and accumulation-kind enums.
  - A model `grain:` matching no entity key silently fell back to first-entity primary
    detection; it now errors with the candidate keys.
  - A typo'd ratio `numerator:`/`denominator:` surfaced as a late compiler error
    (`Unknown metric recipe`) with no location; it now fails at parse naming the metric, the
    field, and close matches. A metric whose kind produces no expression names the per-kind
    required fields instead of `Expression requires a 'kind'`.
  - A measure with neither `kind:` nor `expr:` was misdiagnosed as "missing expr"; the error
    now states both authoring options.
- `validate-config` now runs the warehouse column-reachability probe (previously `check`-only),
  which also probes entity key columns — so a dimension, measure expr, or graph `key:` pointing
  at a nonexistent column fails the first command an author runs instead of query time.
- `doctor` failed every standalone package on a cwd-relative Dockerfile check that carried no
  message; the check is now informational (resolved next to the package), failing checks carry
  a hint, and the report lists `failing_checks` by name.
- A missing `package.seed.source` file raised `INTERNAL_ERROR` with a raw errno and a repo-root
  path the author never wrote; it now raises `INVALID_CONFIG` naming the field and both
  locations checked.
- **Conversion operand filters now compile into SQL.** Ad-hoc filters on conversion
  `base`/`converted` aggregate operands (e.g. base = orders containing Product A, converted = a
  later order containing Product B) previously validated cleanly and were silently dropped from
  the compiled plan. They now lower into the conversion event CTEs (filter dimensions are joined
  via the rewrite-safe path gate; fanout joins act as EXISTS-style "contains" filters because
  matching dedups on the event key). Filter shapes that cannot lower — operand-level `window`
  specs, expression-valued clauses, non-`all` combinators — are rejected with structured
  `CONVERSION_NOT_SUPPORTED` errors instead of being ignored.
- A `window` dict on a plain `aggregate` expression was accepted and silently ignored (a
  "rolling 7-day" ask compiled to an unwindowed aggregate). It is now rejected at bind time with
  a pointer to the supported `rolling`/`prior_period`/`period_to_date` expression kinds.
- Conversion `window.unit` values outside the supported set (e.g. `fortnight`) failed only at
  warehouse execution; they now fail `validate` with the supported unit list.
- `dimension_bindings` with unknown keys, unknown `side` values (previously silently treated as
  base-side), or unsupported `denominator` policies are rejected at parse time.
- Unknown dimensions referenced by conversion `constant_properties` raised an internal
  `KeyError`; they now return a structured `OBJECT_NOT_FOUND`.

### Changed

- The planner resolves explicit historical month ranges ("from January 2017 through June 2017",
  "Jan-Jun 2017", "March 2024") into `time.start`/`time.end` instead of dropping the bounds.
- The planner no longer answers conversion/funnel intents with a confident non-conversion draft:
  when conversion markers are detected and the best draft contains no conversion expression, the
  plan is demoted to `low_confidence` with a `CONVERSION_INTENT_UNREALIZED` why, curated
  conversion metrics to redirect to, and the ad-hoc conversion IR shape.
- The `capabilities` payload documents operand-level conversion filters (shape, EXISTS
  semantics, and what is rejected).
- Semi-additive (stock) measures keyed only by their own time column now reduce to the
  last/first snapshot inside each output bucket instead of summing every snapshot in the
  period. Previously a daily YTD rollup queried at month grain returned the sum of all 31
  daily snapshots (16x overstated; 200x as a scalar) with no warning. Distributional
  aggregations (median/avg/min/max) over such series still aggregate across the snapshots
  in the bucket.
- `diff-package` / `impact-report` snapshots now include measure expressions, source
  relations, row grain, accumulation, and entity tables — redefining a measure's SQL or
  repointing a model at a different relation previously diffed as "0 changes, risk: low".
- Warnings no longer carry refusal-shaped envelope fields (`why_invalid`,
  `unsupported_construct`, …) unless the producer set them explicitly; an advisory caveat
  on a successful query no longer reads like a failed one.
- The wheel ships `semantic_rails` plus `mf2sr`, the MetricFlow translator behind
  `semantic-rails import --from metricflow`; `tests/mf2sr` now runs in the CI, publish, and
  release-readiness gates. The `semantic_layer` rename shim and the `semantic-layer`
  console-script alias were dropped before first publish (nothing was ever published under
  the old names, and the unrelated PyPI project `semantic-layer` owns that import
  namespace).
- `semantic-rails --version` and `semantic_rails.__version__` report the installed version.

### Feature inventory

Everything below describes the full surface as shipped in 0.1.0.

#### Naming

- The import package is `semantic_rails` and the console script is `semantic-rails`, matching
  the `semantic-rails` distribution name. (The project was developed under the `semantic_layer`
  / `semantic-layer` names, but the unrelated PyPI project `semantic-layer` claims the
  `semantic_layer` import namespace, so both were renamed before this first publish. No rename
  shim ships: nothing was ever published under the old names.)

#### Added

**MCP Streamable HTTP**

- Stateless MCP Streamable HTTP served at `/mcp` by the ASGI app, protocol `2025-11-25`: JSON
  responses, `202` notifications, Origin validation, and a 64 KiB body cap.

**Runtime**

- `semantic_rails/` is the only supported runtime: graph-first packages, planner-owned
  mixed-grain rewrites, AST-first compile and explain, supported event-pair and same-store
  conversion metrics, `metric_predicate`, temporal-validity joins, and segments. The CLI accepts
  `--path` on `query`, `compile`, `explain`, and `validate` so authors can run a freshly authored
  package without registering it.
- Authored `relation:` pipelines (`json_explode`, `date_spine`, `anti_join`, …) lower into
  reviewed SQL CTEs at compile time.

**Agent surface (MCP + HTTP)**

- 13 MCP tools mirroring the public API operations — `capabilities`, `catalog`, `discover`,
  `inspect`, `build-options`, `valid-values`, `plan`, `validate`, `compile`, `execute`, and
  `segment-{validate,explain,preview}` — each with what/when/gotcha descriptions. Errors return
  structured envelopes with `recovery_hints` and `closest_matches`; the envelope shape is
  identical across all 13 tools, and every tool either rejects unknown arguments with a
  structured error or warns and ignores them — none silently accepts unknown keys.
- `plan(intent, query?, detail?)` — the single public natural-language planning tool and HTTP
  route. Returns one best Query IR with a `status` discriminator
  (`ok | low_confidence | unrealizable | out_of_scope`), `intent_ir`, and pre-baked
  `next.validate` / `next.valid_values` / `next.ready_for` args; `detail="full"` adds
  alternatives and blocked drafts. `status="ok"` is trustworthy: plan runs `runtime.validate`,
  which also warms the compile cache for the follow-up `compile` call. Backed by a pattern
  registry (`semantic_rails/planner/`) with 6 hand-tuned named patterns, 2 catch-alls, a
  per-package `disabled_patterns` opt-out, and gated coverage/byte/latency benchmarks
  (`scripts/benchmark_plan.py --gate`) plus a catalog-walked generated corpus.
- Planner time windows: relative windows in natural-language intents ("last month", "last 7
  days", "last N weeks/months/quarters/years", "this year", "yesterday") resolve into the Query
  IR's relative range form (`time.range.last`) or calendar bounds instead of being silently
  dropped. When an intent names a window the planner cannot resolve (e.g. "last few weeks",
  "since 2023"), the plan is downgraded to `low_confidence` with a structured
  `TIME_WINDOW_UNRESOLVED` warning naming the phrase and recovery hints listing supported window
  forms, instead of marking an unbounded draft ready to execute. Query grain follows a resolved
  relative window's unit when no explicit grain is requested (e.g. "last 7 days" produces a
  daily series).
- `capabilities` (loop position 0, ~3KB cold-start orientation) at `GET /api/v1/capabilities`
  and as an MCP tool, returning the locked v1 route index, warehouse and package capabilities,
  and `expression_shapes` — a `{name, description, example}` entry for every accepted
  `select[*].expression` kind, shared between the HTTP and MCP surfaces so agents can introspect
  the IR contract on either.
- `catalog?verbosity=summary` (counts + flat ID lists, under 10KB on jaffle_shop) for cold-start
  orientation; `compact` caps each kind at 200 rows and ships `counts` / `counts_total` /
  `truncated`. A pre-compiled `.compiled/manifest.json` catalog manifest (written by
  `validate-config`, fingerprint-invalidated, `--no-manifest` to skip) serves unfiltered default
  catalog calls ~100× faster.
- `discover`: IDF-weighted token scoring on the user-facing surface, a runnable
  `starter_query_patch` on every measure/metric/dimension candidate, and structured
  `cross-entity` match reasons (with a rank penalty) when a dimension's root entity disagrees
  with the top measure/metric candidates. `discover` and `plan` route off-topic intents through
  a relevance floor (`out_of_scope` / `low_relevance`) instead of hallucinating confidently.
- `inspect` starter patches covering `select`, `group_by`, `time`, `order_by`, `where` (seeded
  from declared `value_domain`s, no warehouse round-trip), and `metric_filters`; the
  `metric_filters` scaffold fires on every metric. History-backed cards expose
  `coverage_notes` and `null_bucket_meaning`.
- `build-options`: `next_legal_steps` (the full ordered remaining-step list, not just the next
  step) and `review_priority`-based ranking with topic-derived `why_recommended` for
  empty-query calls.
- Diagnostics: `UNGRAINED_TIME_PROJECTION` warning when `time.temporal_role` is set without a
  grain, group_by, or inline window expression; `EXPRESSION_NORMALIZED_AWAY` silent-drop guard;
  `EMPTY_RESULT_WINDOW` ships `actual_data_coverage` plus the resolved `requested_window` and
  the original `relative_range`; `WINDOWED_TIME_FILTER_UNSUPPORTED` carries ordered
  `drop_time_start` (always safe, first) and `widen_time_window` (computed `suggested_start`,
  bucket-boundary caveat) recovery hints.
- Query IR guardrails: unknown top-level keys are rejected with `INVALID_QUERY` and a
  `USE_CANONICAL_KEY` hint naming the right slot (`filters`→`where`, `having`→`metric_filters`,
  …) on both `normalize_query` and `normalize_partial_query`; misplaced absolute bounds emit
  `MOVE_BOUNDS_TO_TIME_TOP_LEVEL`; `{dimension: ...}` in `select[]` emits
  `MOVE_DIMENSION_TO_GROUP_BY`; underscore-prefixed annotation keys (`_note`) pass through.
- MCP resources (`semantic-rails://capabilities`, `semantic-rails://catalog/{summary,full}`)
  and prompts (`semantic-rails-query-builder`, `-query-review`, `-segment-workflow`).

**Hosting compatibility**

- Pluggable `PolicyContextResolver` and `AuditSink` protocols, `CompiledSqlCache` protocol with
  a public `Runtime.set_compile_cache` seam, in-process `Runtime.reload()`, per-request `limits`
  on the query envelope, env-controlled CORS allow-list, HMAC-based API-key check, redacted SQL
  on `QUERY_EXECUTION_ERROR` (raw SQL requires `debug: true` + the
  `SEMANTIC_RAILS_ALLOW_DEBUG_SQL=1` operator opt-in + the `debug` role), and
  compact-by-default JSON responses. None of this turns Semantic Rails into a hosted product —
  it just removes the forks one would need to host it.

**Authoring UX**

- `check` is the one-command package gate (parse, validate, examples, tests, manifest, optional
  artifact) and probes the warehouse for column reachability so missing columns fail at author
  time. `validate-config` rejects unknown kind values, requires `value_type` on every metric,
  and flags semantic collisions (identical labels/names, ≥80% label-token overlap,
  `search_terms` subsets) between package objects. `STATEMENT_TIMEOUT_NOT_HONORED` is surfaced
  when the configured timeout cannot be enforced.

**Packaging, release, and deployment**

- PyPI distribution named `semantic-rails`; the wheel includes the runtime, CLI, bundled Jaffle
  proof package, seed assets, and the PEP 561 `semantic_rails/py.typed` marker so type checkers
  pick up the package's inline annotations. Development Status classifier is `4 - Beta`;
  `Programming Language :: Python :: 3.14` is declared and the CI test matrix covers 3.14.
- Tag-triggered PyPI publish workflow using `uv build` and PyPI Trusted Publishing (OIDC,
  `pypi` environment), with the full test suite as a release gate and all third-party actions
  SHA-pinned; `dorny/paths-filter` is SHA-pinned in the CI workflow as well.
- Docker hardening: docker-compose binds the API to `127.0.0.1` by default, the base image is
  pinned to an exact patch release (`python:3.12.13-slim-trixie`), Dockerfile and compose
  healthchecks probe the same `/api/v1/ready` endpoint, and both files document
  `SEMANTIC_RAILS_API_KEYS` / `SEMANTIC_RAILS_CORS_ORIGINS` for non-local deployments.
- `jsonschema` added to the dev dependency group so the schema-contract tests run instead of
  skipping silently. The distribution verifier installs the wheel into an isolated venv and runs
  `packages`, `catalog`, and `query`.

**Comparison pack and license**

- Six-layer comparison (Semantic Rails, MetricFlow, Cube, Malloy, Snowflake Semantic Views, KtX)
  with split baseline (q01–q07, where five of six layers score 7 native and Cube takes one
  workaround on q05) and differentiator (q08–q16, where the workaround cost is visible)
  scoreboards plus a methodology disclosure.
- License posture: Apache 2.0 throughout (switched from MIT before first publish). No
  "available source," no non-commercial tier, no separate enterprise SKU in this repo.

#### Changed

- `time.range.last.unit` values `minute` and `hour` were removed from the published Query IR
  schema — the runtime never supported sub-day relative ranges.
- `where[]` and `order_by[]` standardized on `field` as the single key; the prior
  `where[].dimension` shape is not accepted and the error envelope points at `where[N].field`.
  `schemas/query_ir.v1.json` requires `field` to match the runtime.
- `range.last` is strictly an object (`{unit, value}`) in the JSON Schema, the MCP-exported
  QUERY_SCHEMA, and the validator; the string shorthand (`"90 days"`) is rejected with a
  `USE_OBJECT_SHAPE` recovery hint carrying the corrected shape inline.
- Response-shape dedup: `inspect` cards ship only `recommended_dimensions` /
  `recommended_filters` (the `preferred_*` aliases are gone); intent candidates carry only
  `candidate_ir`; `catalog?verbosity=compact` no longer ships `alias_index` (moved to `full`).
- `validate` / `compile` / `execute` tool descriptions enumerate all four positional key
  vocabularies (`select[].as`, bare `group_by` strings, `where[].field`, `order_by[].field`)
  and the accepted select-expression shapes inline, under the 700-char scannable cap.
- Metadata internals split into `semantic_rails/metadata_parts/` (`capabilities`,
  `valid_values`, `scope_gate`); the internal `formulate` package was renamed to `planner`.

#### Fixed

**Agent ergonomics (from a blind-agent evaluation against the live MCP endpoint)**

- MCP `validate`/`compile`/`execute` now default to `verbosity=minimal` at the adapter
  boundary — default envelopes dropped from 91–103KB to 0.7–2.4KB and error envelopes from
  ~62KB to ~3KB, while keeping structured errors, `recovery_hints`, `rendered_sql` (compile),
  and `rows`/`row_count` (execute). Explicit `verbosity` still wins; HTTP v1 defaults unchanged.
- `tools/list` shrank from ~28.1KB to ~15.9KB: the IR cheat-sheet and full Query-IR schema now
  ship once (on `validate`) with the other IR tools pointing at it; stale size hints corrected.
- `MIXED_GRAIN_INVALID` is now recoverable: details carry `compatible_measures` /
  `compatible_dimensions` / `closest_compatible_measure` (registry-only, bounded), a
  `replace_measure` hint names the closest workable measure (e.g. `revenue_usd` x product
  dimensions now points at `item_revenue_usd`), and calendar-date dimensions get a leading
  `use_time_grain` hint with a concrete `time` block instead of a dead end.

**Author ergonomics (from a cold-start authoring evaluation)**

- `semantic-rails init` output now passes `validate-config` with zero errors (the starter
  template used an entity name as `entity_key`, an unresolvable package-relative measure
  reference, and a mapping-form `domain:` the loader misparses).
- `catalog`, `discover`, `inspect`, `valid-values`, `plan`, and `build-options` accept
  `--path`, giving single-file/directory package authors the discovery surface that
  query-error hints already pointed them to.
- `parse-config --path <dir>` on a directory holding only a single-file `package.yml` now
  says to pass `--path <dir>/package.yml` instead of dead-ending on "graph.yml is missing".
- Single-file package artifacts now bundle the referenced seed SQL/post-SQL and sibling
  `examples/`/`tests/` directories (a check-passing artifact previously could not hydrate its
  own warehouse), with a structured error when a declared seed asset is missing;
  `validate-config` writes `.compiled/manifest.json` for single-file packages too.
- Docs re-measured against reality: corrected every envelope-size claim (`catalog full` is
  ~2.3MB, not "200KB+"), the catalog default tier, the compile-explain key paths
  (`explain.chosen_paths.<entity>.candidates`, not `explain.candidates`), authoring validation
  commands (`--path`, not the closed `--package` list), `entity_key` examples, the derived-ID
  grammar, and the directory `package.id` rule.
- `/mcp` on the ASGI app now honors the same bearer API keys as `/api/v1/*` — previously a
  deployment that configured `SEMANTIC_RAILS_API_KEYS` still exposed the MCP endpoint (including
  the `execute` tool) unauthenticated.

**`where` filter compilation**

- `where` filters with `value: null` and a negating op (`!=`, `<>`, `IS NOT`) now compile to
  `IS NOT NULL` instead of the always-empty `col != NULL`; ordering/LIKE ops against null are
  rejected with a structured `INVALID_QUERY` and recovery hint.
- `IN` / `NOT IN` with a bare scalar value no longer character-splits strings
  (`'Philadelphia'` became `IN ('P','h','i',...)`); scalars are normalized to a one-element
  list.
- The schema-advertised `where` ops `IS NULL`, `IS NOT NULL`, and `NOT IN` now compile
  end-to-end; an empty `IN` list compiles to constant `FALSE` (and empty `NOT IN` to `TRUE`)
  instead of erroring.

**`metric_filters` and expression AST**

- A bare comparison/boolean expression in `metric_filters[]` is now treated as the predicate
  itself instead of being compared to the envelope defaults and compiling to the always-false
  `(expr) IS NULL`.
- Boolean `not` expressions in `metric_filters` are now lowered as a real negation
  (`FALSE = (arg)`); previously the un-negated argument was silently emitted, inverting filter
  results. `not` with zero or multiple args is rejected with a structured error and a recovery
  hint to wrap multiple conditions in a single `and`/`or` arg.
- `BooleanExpr.op` is validated at parse time — only `and`, `or`, `not` (case-insensitive,
  normalized to lowercase) are accepted; unsupported ops fail `validate` with a structured
  `INVALID_EXPRESSION_AST` error including `allowed`, `closest_matches`, and `recovery_hints`
  instead of an opaque render-time token error.
- Malformed `metric_filters[]` items and `path_policy` payloads now return structured
  `INVALID_QUERY` errors with recovery hints instead of leaking bare AttributeError/TypeError
  as `INTERNAL_ERROR`.
- `rolling` / `prior_period` / `conversion` window payloads passed as an int (e.g.
  `window: 28`) raise `INVALID_EXPRESSION_AST` (or `CONVERSION_WINDOW_REQUIRED`) with a
  `USE_OBJECT_SHAPE` hint naming the `{unit, value}` shape, instead of crashing with a
  `TypeError` outside the structured-envelope contract.
- Every `_EXPRESSION_SHAPES` example round-trips through `parse_semantic_expression`
  (previously the `rolling`, `cumulative`, `conversion`, and `distribution` examples were
  unparseable), pinned by a test that iterates every shape.

**CLI**

- CLI error envelopes get the same `closest_matches` enrichment as the HTTP and MCP surfaces —
  a typo'd object id (e.g. `semantic-rails inspect --object-id metric.revenu`) suggests
  near-miss ids instead of claiming no matches exist.
- CLI failures print the structured error exactly once under `error` (previously the same issue
  was serialized three times per failure); exit codes and `ok`/`status` fields are unchanged.

**Build and tooling**

- `make release-check` / `make test` run through `uv run` instead of bare `python3`, which
  could not import `semantic_rails`.

#### Removed

- The pre-release `formulate` / `propose` / `parse-intent` / `expand` surface, collapsed into
  the single public `plan` tool and HTTP route (`parse_intent` remains an internal debug
  helper).
- Deprecated HTTP routes `/api/v1/resolve`, `/api/v1/valid-next`, and `/api/v1/classify`, and
  the corresponding CLI subcommands. The MCP-first surface (discover, inspect, build-options,
  plan) covers every previously-supported use case.
- The `explain` MCP tool, HTTP route, CLI subcommand, and `Runtime.explain` method. The full
  explain payload is available under `response["explain"]` on `compile`'s output, and most
  fields are flattened at the response root via `compile_response_metadata`.
- The internal workbench surface; the public v1 HTTP surface is locked at the 16 routes listed
  in [docs/QUERY_API.md](docs/QUERY_API.md#http-routes).
