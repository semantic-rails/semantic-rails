# Methodology

## Fairness Rules

- Use the same source dataset for all runnable layers: the shared DuckDB database built from `data/jaffle_csv` and `data/seed_jaffle.sql`.
- The shared comparison view for `order_lifecycle` is intentionally limited to the hand-authored `raw_order_lifecycle_events` slice so committed q07/q16 evidence remains stable even though the broader seed can derive lifecycle timestamps for every order.
- Keep the semantic scope intentionally small:
  - baseline models: `orders`, `order_items`, `customers`, `stores`
  - stretch models: `customer_history`, `order_lifecycle`, `storefront_sessions`
- Prefer native semantic layer constructs over precomputed marts or handwritten SQL.
- When a layer needs extra modeling, keep the extra work explicit and local to that layer.
- Do not claim runtime support that was not actually executed in this repo on this machine.
- Score edge cases against their intended semantic behavior, not just matching rows. If a layer only reaches the same result by leaning on helper SQL, extra persisted marts, or source-side rollup columns that bypass the intended metric-predicate or conversion semantics, treat that path as `workaround` or `precomputed`, not `native`.
- Keep the narrative honest in both directions. This executed pack emphasizes numeric questions; compiler-surface concerns such as duplicate-alias rejection, metric-time-only or distinct-values planning, and entity-type join contracts should still be called out separately when they are not exercised here.

## Support Labels

- `native`: expressed cleanly with the layer's normal semantic-model constructs and executed.
- `workaround`: executed, but required awkward extra modeling, manual SQL, or a non-idiomatic query path.
- `precomputed`: executed only by introducing extra persisted logic beyond the common comparison shape.
- `doc_backed`: represented fairly from official docs/specs, but not executed locally.
- `unsupported`: could not be represented faithfully or could not be executed without breaking the comparison rules.

## Evidence Captured

For runnable layers, each question should include:

- the semantic config or model snippet used
- the query/request shape
- generated SQL when the tool exposes it
- a normalized result artifact
- a consistency check against the other executed layers after normalization
- notes on any caveats or compromises

## Scale-Up View

The UI compares two model sets:

- `baseline`: 4 models (`orders`, `order_items`, `customers`, `stores`)
- `stretch`: 7 models (baseline plus `customer_history`, `order_lifecycle`, `storefront_sessions`)

The scale-up view highlights:

- authored files
- authored LOC
- relationship/join count
- question coverage
- non-native question count (`workaround` plus `precomputed`)

The scale-up counts intentionally focus on authored semantic model/config files and omit runners, generated artifacts, and setup logs. For single-file layers, the baseline count is the baseline section of that authored model and the stretch count is the full file.

## Boundaries

- Snowflake Semantic Views are executed through the Snowflake CLI connection `semantic_views_trial`.
- For Snowflake, `q01`-`q07` must execute through `SEMANTIC_VIEW(...)`; the edge-capability questions are labeled `workaround` because they execute as verified SQL on the Snowflake comparison tables.
- Cube is implemented locally without Docker because `docker` is not available in this environment.
- KtX is executed through its Python semantic layer (`ktx-sl`) from a local clone at `/tmp/ktx-compare` by default. The benchmark does not score KtX's broader context ingestion, wiki/search, daemon, or MCP flows.
