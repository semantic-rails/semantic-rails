# Semantic Layer Comparison Pack

This workspace compares a small, shared Jaffle slice across six captured semantic layer runs without touching the active `configs/semantic_rails/jaffle_shop` package.

> ## ⚠️ Methodology Disclosure — read this before the scoreboard
>
> **This pack is designed to make the cost of workarounds visible, not to claim general parity.**
>
> Of the 16 questions, 9 are explicit `scope_level: stretch` and 6 of those 9 are tagged `category: edge_capability` — they exercise primitives Semantic Rails ships natively (`metric_predicate`, temporal-validity joins, event-pair conversion, same-store conversion, contextual entity-graph inheritance). MetricFlow, Cube, Malloy, Snowflake Semantic Views, and KtX were never marketed as covering these primitives natively, so their "precomputed" / "workaround" counts reflect the gap introduced by the question slice, not a general capability deficit.
>
> Read this scoreboard as: **"Here is the concrete cost in helper views, customer rollups, query-time filters, and SQL workarounds that each layer pays to answer the same governed predicate, temporal, and same-store-conversion questions."**
>
> The 7 required questions (q01–q07) — baseline + advanced_portable — are where every layer should perform. Five of the six layers score 7 native there; Cube takes one workaround (q05). The 9 stretch questions are where Semantic Rails has first-class support and the others rely on workarounds. See `shared/methodology.md` for the full scoring rules and `shared/questions.yml` for the per-question scope and category metadata.
>
> **One more thing the numeric score does not capture: the shape of the runtime.** An MCP server with `discover → inspect → plan/build-options → valid-values → validate → compile → execute` as separate tools, structured error envelopes carrying `recovery_hints` and `closest_matches`, and a relevance floor inside `discover` and `plan` is not the same primitive as a SQL renderer with a metric registry, even when both layers can answer q01–q07. The q-suite measures capability; the architecture above the suite measures fit for agent workflows. See [`../../docs/CAPABILITIES.md`](../../docs/CAPABILITIES.md) for the public runtime surface that does not depend on the question slice.

## What This Pack Shows

- The captured 16-question runs produce matching normalized outputs across `Semantic Rails`, `MetricFlow`, `Cube`, `Malloy`, `Snowflake Semantic Views`, and `KtX`.
- `Semantic Rails` is the only layer in this workspace that executes the expanded edge-capability suite natively end to end.
- `MetricFlow` remains strong on temporal validity, but the predicate-heavy edge slice and same-store conversion variant depend on helper dbt views in this pack.
- `Cube` is concise on the baseline, but the edge slice quickly turns into customer-rollup filters and helper cubes.
- `Malloy` still handles q05 cleanly and stays compact, but the edge slice resolves through query-level filters and SQL sources rather than governed semantic primitives.
- `Snowflake Semantic Views` now runs as a real executed layer: `q01`-`q07` use `SEMANTIC_VIEW(...)`, while the edge-capability questions run as verified SQL workarounds on the same Snowflake comparison tables.
- `KtX` overlaps strongly on the portable metric/query layer (`q01`-`q07`) and uses SQL-backed KtX sources or query-level filters for the stretch suite.
- The numeric suite is still not the whole story: compiler-surface controls like metric-time-only planning and duplicate-alias rejection are documented separately because this executed pack does not score them directly.

## Layer Status

The scoreboard is split so the **baseline tie** and the **differentiator slice** are scored separately, per the methodology disclosure above. The combined outcome column is preserved for reference, but read the two split tables first.

### Baseline slice — q01–q07 (every layer should pass these)

These 7 questions are `scope_level: required` (4 `baseline`, 3 `advanced_portable`). They cover the core "count, sum, group-by-month" surface every governed semantic layer ships.

| Layer | Version / Basis | Baseline result |
| --- | --- | --- |
| Semantic Rails | workspace runtime | 7 native |
| MetricFlow | `dbt-metricflow 0.11.0`, `dbt-duckdb 1.10.1` | 7 native |
| Cube | `1.6.32` | 6 native, 1 workaround (q05) |
| Malloy | `0.0.52` | 7 native |
| Snowflake Semantic Views | Snowflake CLI + semantic view trial account | 7 native |
| KtX | `ktx-sl 0.13.1` / KtX `a155c0b` | 7 native |

The baseline tie matters: it confirms every layer in the pack ships a working governed surface for the questions every layer was built to answer. The gap shows up in the next table, on questions the other layers were not built for.

### Differentiator slice — q08–q16 (Semantic Rails first-class primitives)

These 9 questions are `scope_level: stretch`. q08–q10 are `differentiator`; q11–q16 are `edge_capability`. They exercise primitives Semantic Rails ships natively (`metric_predicate`, temporal validity, same-store conversion, contextual entity-graph inheritance). The other layers were not built to express these as governed primitives; their counts measure the cost of the workaround.

| Layer | Stretch result | Workaround shape |
| --- | --- | --- |
| Semantic Rails | 9 native | — |
| MetricFlow | 2 native (q08, q16), 7 precomputed | helper dbt views per stretch question |
| Cube | 0 native, 3 workaround, 6 precomputed | customer-rollup filters and helper cubes |
| Malloy | 0 native, 9 workaround | query-level filters and SQL sources |
| Snowflake Semantic Views | 0 native, 9 workaround | verified SQL workarounds against the same comparison tables |
| KtX | 0 native, 9 workaround | SQL-backed sources and query-level filters |

### Combined (for reference)

| Layer | Version / Basis | Local Status | Outcome |
| --- | --- | --- | --- |
| Semantic Rails | workspace runtime | executed | 16 native |
| MetricFlow | `dbt-metricflow 0.11.0`, `dbt-duckdb 1.10.1` | executed | 9 native, 7 precomputed |
| Cube | `1.6.32` | captured execution; offline-verifiable | 6 native, 4 workaround, 6 precomputed |
| Malloy | `0.0.52` | executed | 7 native, 9 workaround |
| Snowflake Semantic Views | Snowflake CLI + semantic view trial account | executed | 7 native, 9 workaround |
| KtX | `ktx-sl 0.13.1` / KtX `a155c0b` | executed | 7 native, 9 workaround |

## Controls Outside The Numeric Suite

- This executed pack now stresses predicate-heavy and multi-clock analytics much more directly, which is why the gap between `Semantic Rails` and the other layers is clearer than in the earlier 10-question version.
- The comparison is intentionally not a full compiler-surface bakeoff. Other layers retain real advantages on adjacent controls (for example, MetricFlow on metric-time-only planning, distinct-values planning, and duplicate-alias rejection); those are out of scope for this pack and not counted against any layer.
- KtX's broader context product is also out of numeric scope here. This pack executes the Python `ktx-sl` semantic layer, not KtX ingestion, wiki/search, daemon, or MCP context flows.
- The corresponding Semantic Rails advantages in this pack are first-class authored and query-time `metric_predicate` behavior, contextual entity-graph inheritance, same-store conversion semantics, and temporal-valid slicing across multiple business clocks.

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

6. Regenerate the shared contracts:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/generate_comparison_contracts.py
   ```

7. Validate deterministic outputs across the runnable layers:

   ```bash
   uv run python comparisons/semantic_layers/shared/scripts/validate_output_consistency.py
   ```

## Where To Start

- Read `shared/methodology.md` for the support labels and fairness rules.
- Read `shared/results/validation/output_consistency.md` for the cross-layer output check.
- Open `shared/capability_matrix.json` for the row-by-row support summary.
- Open `shared/comparison_data.json` for the per-layer excerpts used by the runtime.
