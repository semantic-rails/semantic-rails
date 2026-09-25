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
  customer-month order counts can't be expressed without SQL. KtX's own
  authoring guidance directs a `sql:` source for exactly this case (a
  per-entity derivation such as an `EXISTS` over a time-windowed subset), and
  asks that each one name the gap that forced it; every SQL source here does,
  in its `descriptions`.
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
`/tmp/ktx-compare`; set `KTX_DIR` to use another directory. The runner records
the KtX version, the wheel hash and the Python package versions in its
`summary.json`.
