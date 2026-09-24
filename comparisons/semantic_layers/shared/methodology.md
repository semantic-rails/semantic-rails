# Methodology

## Fairness Rules

- Use the same source dataset for all runnable layers: the shared DuckDB database built from `data/jaffle_csv` and `data/seed_jaffle.sql` by `shared/scripts/bootstrap_shared_duckdb.py`.
- Every layer reads only the `comparison_*` views that script creates. They pass the seed tables through unchanged, except that `comparison_order_items` adds each item's order time, store and customer.
- Each runner records the dataset fingerprint (a hash of the seed files and the view definitions) with its results. The output check reports a capture made on another dataset as stale, separately from the layers that ran on the current one, instead of counting it as a mismatch.
- Keep the semantic scope intentionally small:
  - baseline models: `orders`, `order_items`, `customers`, `stores`
  - stretch models: `customer_history`, `order_lifecycle`, `storefront_sessions`
- Prefer native semantic layer constructs over precomputed marts or handwritten SQL.
- When a layer needs extra modeling, keep the extra work explicit and local to that layer.
- Do not claim runtime support that was not actually executed in this repo on this machine.
- Score edge cases against their intended semantic behavior, not just matching rows. If a layer only reaches the same result by leaning on helper SQL, extra persisted marts, or source-side rollup columns that bypass the intended metric-predicate or conversion semantics, treat that path as `workaround` or `precomputed`, not `native`. The rubric (`rubric.md`) enforces this for every layer, Semantic Rails included.
- Keep the narrative honest in both directions. This executed pack emphasizes numeric questions; compiler-surface concerns such as duplicate-alias rejection, metric-time-only or distinct-values planning, and entity-type join contracts should still be called out separately when they are not exercised here.

## Support Labels

`shared/scripts/apply_rubric.py` assigns every label from each layer's committed artifacts, with the same rules for every layer, Semantic Rails included. Runners record only whether a question executed. [`rubric.md`](rubric.md) states the rules and how each one is detected.

- `unsupported`: the layer didn't execute the question.
- `precomputed`: the answer reads a rollup column that the question declares in `bypass_columns`.
- `workaround`: the answer depends on SQL written by hand for this pack.
- `native`: the answer uses only the layer's own semantic constructs.

The contracts also keep a `doc_backed` count for a layer represented only from official docs. No layer is represented that way today.

## Evidence Captured

For runnable layers, each question should include:

- the semantic config or model snippet used
- the query/request shape
- generated SQL when the tool exposes it
- a normalized result artifact
- a check against the independent answer key (`shared/oracle/`) after normalization, reading each layer's columns through `shared/column_maps.yml`
- notes on any caveats or compromises

## Scale-Up View

The UI compares two model sets:

- `baseline`: 4 models (`orders`, `order_items`, `customers`, `stores`)
- `stretch`: 7 models (baseline plus `customer_history`, `order_lifecycle`, `storefront_sessions`)

The scale-up view counts authored files, authored LOC and relationships/joins. It doesn't count questions or labels; those are scored per question slice.

The scale-up counts intentionally focus on authored semantic model/config files and omit runners, generated artifacts, and setup logs. For single-file layers, the baseline count is the baseline section of that authored model and the stretch count is the full file.

**Caveat:** the counts are not yet uniform across layers. The Semantic Rails count omits `graph.yml`, `core_metrics.yml` and `package.yml`. Don't compare sizes until one script counts every layer's authored files the same way.

## Boundaries

- Snowflake Semantic Views are executed through the Snowflake CLI connection `semantic_views_trial`.
- For Snowflake, `q01`-`q07` must execute through `SEMANTIC_VIEW(...)`. q08-q16 execute as SQL on the Snowflake comparison tables, so the rubric labels them `workaround`, or `precomputed` where they read a declared rollup column (q11, q12).
- Cube 1.6.32 was captured locally without Docker. It can't be reinstalled until its dependency advisories are resolved, so `cube/scripts/replay_sql.py` re-executes its captured SQL on the current dataset, with the session time zone pinned to UTC.
- KtX is executed through its Python semantic layer (`ktx-sl`) from a local clone at `/tmp/ktx-compare` by default. The benchmark does not score KtX's broader context ingestion, wiki/search, daemon, or MCP flows.
