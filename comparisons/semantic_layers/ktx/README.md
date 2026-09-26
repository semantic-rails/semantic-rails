# KtX Comparison Pack

This pack executes the shared Semantic Rails comparison questions through the
KtX Python semantic layer (`ktx-sl`, the `semantic_layer` package), as shipped
in `@kaelio/ktx` 0.16.0 on npm.

KtX is broader than a metric-only semantic runtime: the package includes an
agent context layer, connectors, wiki/search surfaces, MCP tooling, and a Python
semantic layer that compiles modeled sources into SQL. This benchmark uses that
Python semantic layer directly so the generated SQL can run against the same
DuckDB data as the other local comparison layers.

## Scoring Boundary

- `q01`-`q07` use ordinary KtX sources, measures, joins, filters, and aggregate
  locality, so the rubric (`../shared/rubric.md`) labels them `native`.
- `q08`-`q10` and `q13`-`q16` execute through KtX `sql:` sources, which the
  rubric labels `workaround`. KtX 0.16.0 was reviewed for native alternatives:
  its joins are equality-only (`on` accepts `=` conditions only), its measures
  reject window functions, and it has no query-derived sources, so the
  temporal-validity joins, the 7-day conversion windows and the per
  customer-month order counts can't be expressed without SQL. Each SQL source
  names the gap that forced it in its `descriptions`.
- Known gap in this pack's KtX model: the SQL sources are standalone
  per-question fact tables, not small bridge sources joined to `orders` by
  `order_id` (or to sessions by `session_id`). Three re-derive revenue in SQL,
  the q14 source joins `comparison_stores` by hand although `stores` is a
  declared source, the q10 and q13 sources differ only in their `date_trunc`
  grain, and KtX's validator reports the model as 8 disconnected components
  (`../shared/results/ktx/validate.json`). An expert would more likely write one
  bridge source per derivation and query the existing `orders` measures. That
  wouldn't change a label: the rubric labels any non-passthrough SQL source
  `workaround`.
- The frozen-model questions (q17-q24) run with these sources unchanged. Inline measure
  expressions in the query answer q21 (a `sum(case ...)` beside `orders.revenue_usd`) and q22
  (`avg(...)` and `max(...)`), which the rubric labels `native`; q24 filters on the precomputed
  `lifetime_spend_cents`, so it is `precomputed` like q12. q17-q20 and q23 need a model change:
  the conversion windows and the customer-month threshold are inside SQL sources, joins are
  equality-only, and measures, inline ones included, reject window functions
  (`../shared/frozen_model.yml`).
- `q11` and `q12` filter on the precomputed customer rollup columns
  (`lifetime_order_count`, `lifetime_spend_cents`), so the rubric labels them
  `precomputed`. KtX has no construct that computes a per-customer lifetime
  aggregate for a row-level filter.

## Run

From the repository root:

```bash
uv run --with sqlglot==30.19.0 --with pydantic==2.13.5 --with pyyaml==6.0.3 \
  python comparisons/semantic_layers/ktx/scripts/run_questions.py
```

On its first run the runner fetches `@kaelio/ktx@0.16.0` from the npm registry
with `npm pack` (no install, no dependencies), extracts the Python wheel that
the package bundles (`assets/python/kaelio_ktx-0.16.0-py3-none-any.whl`), and
refuses it unless its sha256 matches the one pinned in the runner, which is the
hash in the package's own `assets/python/manifest.json`. The wheel is cached in
`ktx/.cache/` (git-ignored; set `KTX_DIR` to use another directory). Each run
copies the checked bytes into a private temporary directory and imports the
wheel from there. The runner records the KtX version, the wheel hash and the
Python package versions in its `summary.json`.
