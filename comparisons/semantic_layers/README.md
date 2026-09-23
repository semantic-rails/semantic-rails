# Semantic Layer Comparison Pack

This pack asks one question of six semantic layers: can each layer express and run the same 16
questions over one shared Jaffle dataset? It is a capability comparison. It doesn't measure or
compare latency, token use or cost. It runs without touching the active
`configs/semantic_rails/jaffle_shop` package.

## Read This First

- **Output check: 14 of 16 questions return matching normalized outputs across all six layers.**
  q07 and q16 do not, because the layers did not answer them from the same data. On both, the
  other five layers agree with each other and Semantic Rails differs:
  - The pack's shared `comparison_order_lifecycle` view keeps only the 11 hand-authored lifecycle
    rows (1 delivered month). Cube, Malloy, Snowflake Semantic Views and KtX read it.
  - The Semantic Rails pack reads the full `jaffle_order_lifecycle` table instead (59,652 orders,
    12 delivered months).
  - MetricFlow's model also reads the full table, but its committed answers are consistent with a
    capture made before the seed derived lifecycle rows for every order: replaying its committed
    SQL on today's data returns 12 and 32 rows, not 1 and 2.

  Pointing every layer at the same tables is the next change. The per-question report is
  [`shared/results/validation/output_consistency.md`](shared/results/validation/output_consistency.md).
- **9 of the 16 questions target Semantic Rails features.** q08-q16 (`scope_level: stretch`) were
  chosen to exercise primitives Semantic Rails ships: metric predicates, temporal-validity joins,
  event-pair and same-store conversion, and contextual entity-graph inheritance. They are a
  capability showcase, not a ranking. The 7 shared questions (q01-q07, `scope_level: required`)
  are scored separately below.
- **The support labels are provisional.** The Semantic Rails authors wrote every layer's models
  and assigned every label. Semantic Rails is labeled `native` whenever its query validates.
  Every layer answers q11 and q12 from the same precomputed customer columns
  (`lifetime_order_count`, `lifetime_spend_cents`), yet Semantic Rails is labeled `native` there,
  MetricFlow `precomputed` and the other four `workaround`. Several layers are not yet modeled with
  native features they ship: MetricFlow conversion metrics and metric filters, Cube multi-fact
  queries, multi-stage measures and subquery dimensions, Malloy arbitrary-condition joins and
  query-derived join sources, and Snowflake range joins; KtX's `ktx-sl` hasn't been reviewed for
  native alternatives. The labels are also inconsistent with each other: Cube's q08 uses an
  ordinary declared join that carries the validity condition, yet it is labeled `workaround`,
  while MetricFlow's validity-windowed join is labeled `native`. An independent answer key, an executable
  labeling rubric and idiomatic models for each layer are in progress. Until they land, nothing in
  this pack shows that Semantic Rails is better at q08-q16.

## Versions And Captures

| Layer | Version | Captured (UTC) | Re-runnable from this repo |
| --- | --- | --- | --- |
| Semantic Rails | 0.2.1 | 2026-09-23 | yes |
| MetricFlow | `dbt-metricflow 0.11.0`, `dbt-duckdb 1.10.1` | by 2026-06-24 (exact date not recorded) | yes; installs pinned packages |
| Cube | `1.6.32` | 2026-04-07 | no; captured evidence only, until the captured lockfile's dependency advisories are resolved |
| Malloy | `@malloydata/cli 0.0.52` | by 2026-06-24 (exact date not recorded) | yes; installs the pinned CLI |
| Snowflake Semantic Views | Snowflake CLI + semantic view trial account | 2026-04-07 | needs a live Snowflake account |
| KtX | `ktx-sl 0.13.1` / KtX `a155c0b` | by 2026-06-24 (exact date not recorded) | yes; clones KtX at `a155c0b` |

Dates are UTC. Cube's results record `lastRefreshTime` 2026-04-07T03:04:57Z, and Snowflake's
summary records `2026-04-06T23:05:57-04:00`. The other June captures predate the consistency
report generated at 2026-06-24T03:47:13Z. The Semantic Rails runner records its engine version,
commit and run timestamp in `shared/results/semantic_rails/summary.json`.

## Shared Questions: q01-q07

These 7 questions (`scope_level: required`; 4 `baseline`, 3 `advanced_portable`) cover the count,
sum and group-by-month surface every layer in the pack was built to answer.

| Layer | Support labels (provisional) |
| --- | --- |
| Semantic Rails | 7 native |
| MetricFlow | 7 native |
| Cube | 6 native, 1 workaround (q05, through a helper cube; Cube's multi-fact queries not modeled yet) |
| Malloy | 7 native |
| Snowflake Semantic Views | 7 native |
| KtX | 7 native |

Output check: 6 of 7 match across all six layers. q07 does not; see *Read This First*.

## Semantic-Rails-Targeted Questions: q08-q16

These 9 questions (`scope_level: stretch`; q08-q10 `differentiator`, q11-q16 `edge_capability`)
were chosen to exercise primitives Semantic Rails ships. The table records how this pack models
each layer today. It is not a ranking.

| Layer | Support labels (provisional) | How this pack models the layer |
| --- | --- | --- |
| Semantic Rails | 9 native | Semantic-model primitives; q11 and q12 read the same precomputed customer columns as every other layer |
| MetricFlow | 2 native (q08, q16), 7 precomputed | Helper dbt views; native conversion metrics and metric filters not modeled yet |
| Cube | 3 workaround, 6 precomputed | q08 through a declared join carrying the validity condition; q09-q16 through helper cubes or joined rollup filters; multi-fact queries, multi-stage measures and subquery dimensions not modeled yet |
| Malloy | 9 workaround | SQL sources and query-level filters; arbitrary-condition joins and query-derived join sources not modeled yet |
| Snowflake Semantic Views | 9 workaround | SQL on the same tables outside `SEMANTIC_VIEW(...)`; range joins not modeled yet |
| KtX | 9 workaround | SQL-backed sources and query-level filters |

Output check: 8 of 9 match across all six layers. q16 does not; see *Read This First*.

## What This Pack Does Not Measure

- Performance. Latency, compile time, token use and cost aren't compared. The Semantic Rails
  evidence files carry compile timings, but nothing compares them across layers.
- Compiler-surface controls. Other layers have real advantages here that this pack does not
  score, for example MetricFlow's metric-time-only planning, distinct-values planning, and
  duplicate-alias rejection.
- Warehouse coverage, maturity, BI integrations and agent-workflow fit. For the Semantic Rails
  runtime surface itself, see [`../../docs/CAPABILITIES.md`](../../docs/CAPABILITIES.md).
- KtX's broader context product. This pack executes the Python `ktx-sl` semantic layer, not KtX
  ingestion, wiki/search, daemon, or MCP context flows.

## Snowflake MCP Execution

- Snowflake MCP smoke runs should validate planned Query IR and compiled SQL, not pre-authored contextual metric IDs.
- For qualified metric questions, first call `/plan` and assert `best.interpreted_intent.pattern == "qualified_metric_rollup"`; then compile the returned `best.query_ir`.
- Record `question_id`, `layer`, `status`, `elapsed_seconds`, `row_count`, `sql_path`, and any errors for each compiled query.
- Mark result parity only when both the semantic layer and the comparison layer execute successfully against the same Snowflake comparison tables.

## Structure

- `shared/`
  Canonical question suite, methodology, generated JSON contracts, bootstrap scripts, and captured results.
- `semantic_rails/`
  Comparison-only package for this repo's semantic runtime.
- `metricflow/`
  Minimal dbt + MetricFlow project on the shared DuckDB dataset.
- `cube/`
  Captured Cube Core models, queries, runner source, results, non-installable original lock graph, raw audit, normalized SBOM, and an offline verifier.
- `malloy/`
  Minimal Malloy project with native baseline queries and SQL-source stretch workarounds.
- `snowflake_semantic_views/`
  Executed Snowflake Semantic Views pack, trial-account setup assets, and query runner.
- `ktx/`
  Executed KtX `ktx-sl` pack with native baseline sources and SQL-source stretch workarounds.

## Merge Hygiene

- Rebuildable local runtime state is intentionally ignored:
  - `shared/data/jaffle_comparison.duckdb`
  - `metricflow/.venv`, `metricflow/logs`, `metricflow/target`
  - `cube/node_modules`, `cube/.cubestore`
  - `malloy/node_modules`, `malloy/.home`, `malloy/.cache`
  - `snowflake_semantic_views/trial_data/*.csv`
- Executed result artifacts under `shared/results/` are kept because they are part of the comparison evidence.

## Reproduce

1. Build the shared DuckDB:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/bootstrap_shared_duckdb.py
   ```

2. Execute the local runtime:

   ```bash
   uv run python comparisons/semantic_layers/semantic_rails/scripts/run_questions.py
   ```

3. Execute the external runnable layers. Cube remains captured evidence only
   until its upstream npm graph is free of unresolved high/critical advisories:

   ```bash
   uv run python comparisons/semantic_layers/metricflow/scripts/run_questions.py
   uv run python comparisons/semantic_layers/malloy/scripts/run_questions.py
   test -d /tmp/ktx-compare || git clone https://github.com/Kaelio/ktx /tmp/ktx-compare
   git -C /tmp/ktx-compare checkout a155c0b
   PYTHONPATH=/tmp/ktx-compare/python/ktx-sl \
     uv run --with sqlglot --with pydantic --with pyyaml \
     python comparisons/semantic_layers/ktx/scripts/run_questions.py
   ```

4. Verify the Cube capture without installing its vulnerable npm graph:

   ```bash
   python3 comparisons/semantic_layers/cube/scripts/verify_evidence.py
   ```

5. Rebuild the Snowflake trial pack with the default `semantic_views_trial` connection:

   ```bash
   snow sql -c semantic_views_trial -f comparisons/semantic_layers/snowflake_semantic_views/trial_setup.sql
   bash comparisons/semantic_layers/snowflake_semantic_views/scripts/export_trial_csvs.sh
   bash comparisons/semantic_layers/snowflake_semantic_views/scripts/upload_trial_csvs.sh
   uv run python comparisons/semantic_layers/snowflake_semantic_views/scripts/run_questions.py
   ```

6. Validate deterministic outputs across the runnable layers:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/validate_output_consistency.py
   ```

7. Regenerate the shared contracts. They read the validation report, so run this last:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/generate_comparison_contracts.py
   ```

## Where To Start

- Read `shared/methodology.md` for the support labels and fairness rules.
- Read `shared/results/validation/output_consistency.md` for the cross-layer output check.
- Open `shared/capability_matrix.json` for the row-by-row support summary and the generated
  claims.
- Open `shared/comparison_data.json` for the per-layer excerpts and headline findings.
