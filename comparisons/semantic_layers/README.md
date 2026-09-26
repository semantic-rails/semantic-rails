# Semantic Layer Comparison Pack

This pack asks one question of six semantic layers: can each layer express and run the same 24
questions over one shared Jaffle dataset? It is a capability comparison. It doesn't measure or
compare latency, token use or cost. It runs without touching the active
`configs/semantic_rails/jaffle_shop` package.

## Read This First

- **With its model frozen, how many metric variants can each layer answer?** q01-q16 were
  answered with metrics written for them, in every layer. q17-q24 each change one parameter of a
  metric those models already use: a 14-day instead of a 7-day conversion window, a 50-minute
  same-store window, a trailing 3-month window, the prior month's value, large-order revenue
  beside total revenue, the average and maximum of a measure modeled as a sum, and two new
  thresholds. Every layer answers them with its model exactly as written for q01-q16, through
  its query-time interface only. Answered, out of 8: **Malloy 8, Semantic Rails 6, Cube 6 (4 of
  them through SQL API workarounds), KtX 3 and MetricFlow 2**; each other answer needs a model
  change, for the reason in [`shared/frozen_model.yml`](shared/frozen_model.yml). Snowflake
  Semantic Views isn't assessed. See *Frozen-Model Questions* below.
- **Output check: on all 16 questions, the five layers checked on the current dataset return an
  independent answer key's normalized outputs, with numbers matching within 1e-6** (Semantic
  Rails, MetricFlow, Cube, Malloy and KtX), and on the frozen-model questions every answer a
  layer executed matches it too. No layer is the reference: the answer key is SQL
  written against the same views without seeing any layer's models or outputs (see *Independent
  Answer Key* below). Every layer reads the same `comparison_*` views, and all five ran live on
  2026-09-26 for this capture.
- **Snowflake Semantic Views is a stale April capture.** It ran on 2026-04-07 on an earlier
  dataset, whose lifecycle view held only the 11 hand-authored lifecycle rows, and it can't be
  re-run or re-authored without a live account. The output check reports it separately: it matches the answer key on
  14 questions and differs on q07 and q16, the two questions that read lifecycle data. The
  per-question report is
  [`shared/results/validation/output_consistency.md`](shared/results/validation/output_consistency.md).
- **This data can't test every intended semantic.** Delivered time never moves an order into
  another month, no customer orders at two stores, all 10 sessions are at one store on one day,
  and customer history covers 4 customers. Only Cube and Malloy apply q09's and q15's 7-day
  window boundaries exactly as the rule states, `(started_at, started_at + 7 days]`. Semantic
  Rails, MetricFlow (at minute grain) and KtX count `[started_at, started_at + 7 days)`, the
  duration convention the frozen-model answer keys use, and no order in the current dataset
  falls on either boundary; this pack's Snowflake Semantic Views SQL counts the same window on
  its earlier dataset. Each layer's weaknesses in `shared/comparison_data.json` say so. On q07, q09, q14, q15 and q16 in particular, matching the
  answer key is weak evidence that a layer implements the intended rule; see
  [`shared/oracle/SEMANTICS.md`](shared/oracle/SEMANTICS.md).
- **9 of the 16 questions target Semantic Rails features.** q08-q16 (`scope_level: stretch`) were
  chosen to exercise primitives Semantic Rails ships: metric predicates, temporal-validity joins,
  event-pair and same-store conversion, and contextual entity-graph inheritance. They are a
  capability showcase, not a ranking. The 7 shared questions (q01-q07, `scope_level: required`)
  are scored separately below.
- **Every support label comes from one executable rubric.** `shared/scripts/apply_rubric.py`
  applies the same rules to every layer, Semantic Rails included, and publishes the evidence for
  each label; [`shared/rubric.md`](shared/rubric.md) states the rules. Semantic Rails,
  Snowflake Semantic Views and KtX answer q11 and q12 from the precomputed customer columns
  (`lifetime_order_count`, `lifetime_spend_cents`), so they are labeled `precomputed` there;
  MetricFlow, Cube and Malloy compute those rollups from orders and are labeled `native`.
- **Every runnable competitor is modeled idiomatically on its current version.** The Semantic
  Rails authors wrote every layer's models. MetricFlow, Cube, Malloy and KtX were re-authored
  with the features they ship: MetricFlow conversion metrics and metric filters, Cube
  multi-fact queries, multi-stage measures and subquery dimensions, Malloy arbitrary-condition
  joins and query-derived sources. KtX's `ktx-sl` was reviewed for native alternatives: its
  joins are equality-only and it has no query-derived sources, so its `workaround` labels stand.
  Its SQL sources are standalone per-question fact tables rather than bridge sources joined to
  `orders`, a known gap in this pack's KtX model (see `ktx/README.md`). Snowflake range joins
  aren't modeled, because the capture can't be re-run. A `workaround` label describes this
  pack's model of a layer, not the layer itself. Nothing in this pack claims that any layer,
  Semantic Rails included, is better than another.

## Versions And Captures

| Layer | Version | Captured (UTC) | Re-runnable from this repo |
| --- | --- | --- | --- |
| Semantic Rails | 0.3.1, not a release (engine tree `dd85761`, `main` after v0.3.1) | 2026-09-26 | yes |
| MetricFlow | `dbt-metricflow 0.15.0` (`metricflow 0.213.0`), `dbt-core 1.12.5`, `dbt-duckdb 1.11.0` (`metricflow/requirements.lock`) | 2026-09-26 | yes; installs the locked packages |
| Cube | Cube Core `1.7.45` (`@cubejs-backend/server`, `@cubejs-backend/duckdb-driver`; `cube/package-lock.json`) | 2026-09-26 | yes, on darwin-arm64 (the only platform whose native binary is pinned); installs the locked packages and starts Cube locally |
| Malloy | `@malloydata/cli 0.0.57` (`malloy/package-lock.json`) | 2026-09-26 | yes; installs the locked CLI |
| Snowflake Semantic Views | Snowflake CLI + semantic view trial account | stale: 2026-04-07, on an earlier dataset | needs a live Snowflake account |
| KtX | `@kaelio/ktx 0.16.0` (its bundled `ktx-sl` wheel, pinned by sha256) | 2026-09-26 | yes; fetches the npm package and checks the wheel's hash |

Dates are UTC. Snowflake's summary records `2026-04-06T23:05:57-04:00`. Each runner records its tool versions,
run timestamp and dataset fingerprint in its `summary.json` under `shared/results/`. The
fingerprint hashes the seed files and the `comparison_*` view definitions, so the output check
can tell a capture made on other data from a real mismatch.

The Semantic Rails runner also records the source trees of its engine, its package, its queries
and runner, and the question suite, and whether the engine is exactly a tagged release. The
committed Semantic Rails evidence ran on an engine after the v0.3.1 release: its engine tree,
`dd85761`, is `main`'s engine tree at `b2bb05a`, not `git rev-parse v0.3.1:semantic_rails`, so it is
labeled "0.3.1, not a release (engine tree dd85761)" wherever the version is shown. The commit it
records may not survive a squash merge, but the tree hashes do.

## Shared Questions: q01-q07

These 7 questions (`scope_level: required`; 4 `baseline`, 3 `advanced_portable`) cover the count,
sum and group-by-month surface every layer in the pack was built to answer.

| Layer | Support labels |
| --- | --- |
| Semantic Rails | 7 native |
| MetricFlow | 7 native |
| Cube | 7 native |
| Malloy | 7 native |
| Snowflake Semantic Views | 7 native |
| KtX | 7 native |

Output check: 7 of 7 match the answer key across the five layers checked on the current dataset.

## Semantic-Rails-Targeted Questions: q08-q16

These 9 questions (`scope_level: stretch`; q08-q10 `differentiator`, q11-q16 `edge_capability`)
were chosen to exercise primitives Semantic Rails ships. The table records how this pack models
each layer today. It is not a ranking.

| Layer | Support labels | How this pack models the layer |
| --- | --- | --- |
| Semantic Rails | 7 native, 2 precomputed (q11, q12) | Semantic-model primitives; q11 and q12 filter on the precomputed customer columns |
| MetricFlow | 9 native | Validity-windowed semantic models (q08, q16), conversion metrics with constant properties (q09, q15), and metric filters (q10-q14); q10, q13 and q14 group their filters by surrogate entities defined with `expr` |
| Cube | 9 native | Declared joins carrying validity and 7-day windows (q08, q09, q15, q16), subquery dimensions (q09, q11, q12, q15), and multi-stage measures at a fixed customer-month or customer-store-month grain (q10, q13, q14) |
| Malloy | 9 native | Arbitrary-condition joins (q08, q09, q15, q16) and query-derived sources joined back to orders (q10-q14) |
| Snowflake Semantic Views | 7 workaround, 2 precomputed | Stale capture: SQL on the same tables outside `SEMANTIC_VIEW(...)`; range joins not modeled, since the capture can't be re-run |
| KtX | 7 workaround, 2 precomputed | SQL-backed sources (its joins are equality-only; they are standalone per-question fact tables, a known gap described in `ktx/README.md`), and query-level filters on the rollup columns for q11 and q12 |

Output check: 9 of 9 match the answer key across the five layers checked on the current dataset.

## Frozen-Model Questions: q17-q24

These 8 questions (`scope_level: variant`) each change one parameter of a metric that q01-q16
already use, and no layer's model defines the variant. Every layer answers them with its model
unchanged (the rubric checks each model against its sha256 in `shared/frozen_model.yml`), through
its documented query-time interface only. A layer that can't express a variant is labeled
`requires_model_change`, with the reason and a documentation link in `shared/frozen_model.yml`.

| Layer | Support labels | Answered with the model frozen | What answers them, or why not |
| --- | --- | --- | --- |
| Semantic Rails | 6 native, 2 requires_model_change (q19, q20) | 6 of 8 | Query API conversion windows, aggregate overrides, a scoped aggregate and metric predicates. `rolling` and `prior_period` run over a dense calendar, and this package declares no calendar entity, so q19 and q20 need one |
| MetricFlow | 2 native (q23, q24), 6 requires_model_change (q17-q22) | 2 of 8 | `--where` metric filters over existing entities answer the new thresholds. A conversion window, a cumulative window, a period offset, a per-metric filter and an aggregation are each part of a metric's definition |
| Cube | 2 native (q22, q24), 4 workaround (q19-q21, q23), 2 requires_model_change (q17, q18) | 6 of 8 | A REST filter answers q24, and an SQL API query's `AVG` and `MAX` over the item revenue measure answer q22 (Cube pushes them down as aggregates of the measure's row expression). SQL API queries answer q19, q20, q21 and q23 by wrapping a Cube query in SQL (window functions, a derived table). The 7-day window is on a declared join, the SQL API joins cubes only along declared joins, and the model hides the session key a narrower window would group by |
| Malloy | 8 native | 8 of 8 | Filtered and ad hoc aggregates, calculations (`sum_moving`, `lag`) and, for q17 and q18, a join the query declares on the model's source |
| KtX | 2 native (q21, q22), 1 precomputed (q24), 5 requires_model_change (q17-q20, q23) | 3 of 8 | Inline measure expressions answer q21 and q22, and a filter on the precomputed spend column answers q24. The windows and the customer-month threshold are inside SQL sources, joins are equality-only and measures reject window functions |
| Snowflake Semantic Views | 8 not_assessed | not assessed | A stale capture: no variant can be run without a live account |

Output check: every one of the 25 answers the layers executed matches the answer key.

What this doesn't show:

- **The label says how, not only whether.** Malloy answers q17 and q18 by declaring a windowed
  join in the query itself; it is Malloy syntax, not SQL, so the rubric labels it `native`. Cube's
  four SQL API answers that wrap a Cube query in SQL are `workaround`, while its SQL API `AVG`
  and `MAX` over a measure (q22) select members only and are `native`. q19's moving sum
  and q20's `lag` in Malloy and Cube step over month rows, which equals the calendar rule here
  only because every month has orders.
- **Some variants don't discriminate on this data.** q17's 14-day rate equals q09's 7-day rate
  (every session converts within an hour), and q22's maxima are each product type's top price in
  every month, so a layer returning the base metric would still match there
  ([`shared/oracle/SEMANTICS.md`](shared/oracle/SEMANTICS.md)). The labels don't depend on it:
  each layer's query for a variant is in its `queries/` folder.
- **The set is small, and the Semantic Rails authors chose it** knowing which parameters Semantic
  Rails composes at query time and which some other layers set in the model. It probes where
  each layer's frozen-model boundary lies; it isn't a ranking, and it doesn't weigh what a model
  change costs in each layer.

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
  Cube Core project: models, REST and SQL API queries, the locked npm install, its recorded `npm audit`, a live runner, and an offline check of the install surface.
- `malloy/`
  Minimal Malloy project: one model with a named query per question for q01-q16, and one query file per frozen-model question that imports the unchanged model.
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

3. Execute the external runnable layers. Cube needs its locked npm install first, plus the
   prebuilt native binary that one `postinstall` downloads and `cube/index.js` pins by sha256
   (see [`cube/README.md`](cube/README.md)):

   ```bash
   (cd comparisons/semantic_layers/cube && CUBESTORE_SKIP_POST_INSTALL=true npm ci --ignore-scripts \
     && npm rebuild @cubejs-backend/native)
   uv run python comparisons/semantic_layers/metricflow/scripts/run_questions.py
   uv run python comparisons/semantic_layers/cube/scripts/run_questions.py
   uv run python comparisons/semantic_layers/malloy/scripts/run_questions.py
   uv run --with sqlglot==30.19.0 --with pydantic==2.13.5 --with pyyaml==6.0.3 \
     python comparisons/semantic_layers/ktx/scripts/run_questions.py
   ```

   Run them one at a time: MetricFlow's `dbt build` writes to the shared DuckDB, and DuckDB
   allows one writer at a time.

4. Check the Cube install surface offline (exact pins, registry-only lockfile, and a recorded
   `npm audit` with no high or critical advisory):

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
