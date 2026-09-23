# Semantic Layer Comparison Pack

This pack asks one question of six semantic layers: can each layer express and run the same 16
questions over one shared Jaffle dataset? It is a capability comparison. It doesn't measure or
compare latency, token use or cost. It runs without touching the active
`configs/semantic_rails/jaffle_shop` package.

## Read This First

- **Output check: on all 16 questions, the five layers run on the current dataset return an
  independent answer key's normalized outputs, with numbers matching within 1e-6** (Semantic
  Rails, MetricFlow, Cube, Malloy and KtX). No layer is the reference: the answer key is SQL
  written against the same views without seeing any layer's models or outputs (see *Independent
  Answer Key* below). Every layer reads the same `comparison_*` views. Cube wasn't re-run: its
  answers come from re-executing the SQL that Cube 1.6.32 generated on 2026-04-07, because Cube
  itself can't be reinstalled until the captured lockfile's dependency advisories are resolved.
  On every question whose data didn't change, that replay returns the rows Cube returned, with
  numbers equal to within 1e-9.
- **Snowflake Semantic Views is a stale capture.** It ran on 2026-04-07 on an earlier dataset,
  whose lifecycle view held only the 11 hand-authored lifecycle rows, and it can't be re-run
  without a live account. The output check reports it separately: it matches the answer key on
  14 questions and differs on q07 and q16, the two questions that read lifecycle data. The
  per-question report is
  [`shared/results/validation/output_consistency.md`](shared/results/validation/output_consistency.md).
- **This data can't test every intended semantic.** Delivered time never moves an order into
  another month, no customer orders at two stores, all 10 sessions are at one store on one day,
  and customer history covers 4 customers. On q07, q14, q15 and q16 in particular, matching the
  answer key is weak evidence that a layer implements the intended rule; see
  [`shared/oracle/SEMANTICS.md`](shared/oracle/SEMANTICS.md).
- **9 of the 16 questions target Semantic Rails features.** q08-q16 (`scope_level: stretch`) were
  chosen to exercise primitives Semantic Rails ships: metric predicates, temporal-validity joins,
  event-pair and same-store conversion, and contextual entity-graph inheritance. They are a
  capability showcase, not a ranking. The 7 shared questions (q01-q07, `scope_level: required`)
  are scored separately below.
- **Every support label comes from one executable rubric.** `shared/scripts/apply_rubric.py`
  applies the same rules to every layer, Semantic Rails included, and publishes the evidence for
  each label; [`shared/rubric.md`](shared/rubric.md) states the rules. Every layer answers q11
  and q12 from the precomputed customer columns (`lifetime_order_count`, `lifetime_spend_cents`),
  so all six are labeled `precomputed` there. The Semantic Rails authors wrote every layer's
  models, and several layers are not yet modeled with native features they ship: MetricFlow
  conversion metrics and metric filters, Cube multi-fact queries, multi-stage measures and
  subquery dimensions, Malloy arbitrary-condition joins and query-derived join sources, and
  Snowflake range joins. KtX's `ktx-sl` hasn't been reviewed for native alternatives. A
  `workaround` label describes this pack's model of a layer, not the layer itself. Until every
  layer is modeled with the features it ships, nothing in this pack shows that Semantic Rails is
  better at q08-q16.

## Versions And Captures

| Layer | Version | Captured (UTC) | Re-runnable from this repo |
| --- | --- | --- | --- |
| Semantic Rails | 0.2.1 | 2026-09-23 | yes |
| MetricFlow | `dbt-metricflow 0.11.0`, `dbt-duckdb 1.10.1` (`metricflow/requirements.lock`) | 2026-09-23 | yes; installs the locked packages |
| Cube | `1.6.32` | SQL captured 2026-04-07; re-executed 2026-09-23 | the captured SQL re-executes; Cube itself can't be reinstalled until the captured lockfile's dependency advisories are resolved |
| Malloy | `@malloydata/cli 0.0.52` | 2026-09-23 | yes; installs the locked CLI |
| Snowflake Semantic Views | Snowflake CLI + semantic view trial account | 2026-04-07, on an earlier dataset | needs a live Snowflake account |
| KtX | `ktx-sl 0.13.1` / KtX `a155c0b` | 2026-09-23 | yes; clones KtX at `a155c0b` |

Dates are UTC. Cube's captured results record `lastRefreshTime` 2026-04-07T03:04:57Z, and
Snowflake's summary records `2026-04-06T23:05:57-04:00`. Each runner records its tool versions,
run timestamp and dataset fingerprint in its `summary.json` under `shared/results/`. The
fingerprint hashes the seed files and the `comparison_*` view definitions, so the output check
can tell a capture made on other data from a real mismatch.

The Semantic Rails runner also records the source trees of its engine, its package, its queries
and runner, and the question suite, and whether the engine is exactly a tagged release. The
committed Semantic Rails evidence ran on the v0.2.1 engine: its engine tree equals
`git rev-parse v0.2.1:semantic_rails`. The commit it records may not survive a squash merge, but
the tree hashes do. Re-running from a later commit whose engine differs is labeled "0.2.1, not a
release (engine tree …)" wherever the version is shown.

## Shared Questions: q01-q07

These 7 questions (`scope_level: required`; 4 `baseline`, 3 `advanced_portable`) cover the count,
sum and group-by-month surface every layer in the pack was built to answer.

| Layer | Support labels |
| --- | --- |
| Semantic Rails | 7 native |
| MetricFlow | 7 native |
| Cube | 6 native, 1 workaround (q05, through a helper cube; Cube's multi-fact queries not modeled yet) |
| Malloy | 7 native |
| Snowflake Semantic Views | 7 native |
| KtX | 7 native |

Output check: 7 of 7 match across the five layers run on the current dataset.

## Semantic-Rails-Targeted Questions: q08-q16

These 9 questions (`scope_level: stretch`; q08-q10 `differentiator`, q11-q16 `edge_capability`)
were chosen to exercise primitives Semantic Rails ships. The table records how this pack models
each layer today. It is not a ranking.

| Layer | Support labels | How this pack models the layer |
| --- | --- | --- |
| Semantic Rails | 7 native, 2 precomputed (q11, q12) | Semantic-model primitives; q11 and q12 filter on the precomputed customer columns |
| MetricFlow | 2 native (q08, q16), 5 workaround, 2 precomputed | Validity-windowed semantic models for q08 and q16, helper dbt views for the rest; native conversion metrics and metric filters not modeled yet |
| Cube | 1 native (q08), 6 workaround, 2 precomputed | q08 through a declared join carrying the validity condition, helper cubes for q09, q10 and q13-q16, and filters on joined rollup columns for q11 and q12; multi-fact queries, multi-stage measures and subquery dimensions not modeled yet |
| Malloy | 7 workaround, 2 precomputed | SQL sources, and query-level filters on the rollup columns for q11 and q12; arbitrary-condition joins and query-derived join sources not modeled yet |
| Snowflake Semantic Views | 7 workaround, 2 precomputed | SQL on the same tables outside `SEMANTIC_VIEW(...)`; range joins not modeled yet |
| KtX | 7 workaround, 2 precomputed | SQL-backed sources, and query-level filters on the rollup columns for q11 and q12 |

Output check: 9 of 9 match across the five layers run on the current dataset.

## Independent Answer Key

`shared/oracle/` holds one SQL query per question, written directly against the shared
`comparison_*` views; no layer generated it. An agent wrote it from `questions.yml`, the view
definitions and the raw data, without seeing any layer's models, SQL or outputs. It derives
"first order", "lifetime", "more than 10 orders in the month" and "converted within 7 days" from
order and session facts instead of the precomputed flags and rollups. A second agent that didn't
write it reviewed every query against the question text and re-derived the answers independently.
[`shared/oracle/SEMANTICS.md`](shared/oracle/SEMANTICS.md) states the rule for each question and
every interpretation choice.

`shared/scripts/run_oracle.py` runs the answer key on the current dataset. The output check
compares every layer, Semantic Rails included, with it, and refuses an answer key whose data,
queries or questions have changed since it ran. A test re-runs every answer-key query and
compares it with the committed answers. Each layer's result columns are mapped to
the answer's fields explicitly in `shared/column_maps.yml`; a missing column fails the check
instead of being guessed from its name.

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
- Executed result artifacts under `shared/results/` are kept because they are part of the comparison evidence. `shared/results/cube/` is Cube's pinned capture; `shared/results/cube_sql_replay/` re-executes its SQL on the current dataset.

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
     uv run --with sqlglot==30.19.0 --with pydantic==2.13.4 --with pyyaml==6.0.3 \
     python comparisons/semantic_layers/ktx/scripts/run_questions.py
   ```

4. Verify the Cube capture without installing its vulnerable npm graph, then re-execute its
   captured SQL on the current dataset:

   ```bash
   python3 comparisons/semantic_layers/cube/scripts/verify_evidence.py
   uv run python comparisons/semantic_layers/cube/scripts/replay_sql.py
   ```

5. Rebuild the Snowflake trial pack with the default `semantic_views_trial` connection:

   ```bash
   snow sql -c semantic_views_trial -f comparisons/semantic_layers/snowflake_semantic_views/trial_setup.sql
   bash comparisons/semantic_layers/snowflake_semantic_views/scripts/export_trial_csvs.sh
   bash comparisons/semantic_layers/snowflake_semantic_views/scripts/upload_trial_csvs.sh
   uv run python comparisons/semantic_layers/snowflake_semantic_views/scripts/run_questions.py
   ```

6. Answer every question with the independent answer key, then check every layer against it:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/run_oracle.py
   uv run python comparisons/semantic_layers/shared/scripts/validate_output_consistency.py
   ```

7. Label every answer with the rubric:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/apply_rubric.py
   ```

8. Regenerate the shared contracts. They read the validation report and the rubric labels, so
   run this last:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/generate_comparison_contracts.py
   ```

## Where To Start

- Read `shared/methodology.md` for the fairness rules.
- Read `shared/rubric.md` for how every support label is assigned.
- Read `shared/oracle/SEMANTICS.md` for the answer key's rule for each question.
- Read `shared/results/validation/output_consistency.md` for each layer's check against the answer key.
- Open `shared/capability_matrix.json` for the row-by-row support summary and the generated
  claims.
- Open `shared/comparison_data.json` for the per-layer excerpts and headline findings.
