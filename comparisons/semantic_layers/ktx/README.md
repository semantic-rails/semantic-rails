# KtX Comparison Pack

This pack executes the shared Semantic Rails comparison questions through the
KtX Python semantic layer (`ktx-sl`) at `/tmp/ktx-compare/python/ktx-sl`.

KtX is broader than a metric-only semantic runtime: the repository includes an
agent context layer, connectors, wiki/search surfaces, MCP tooling, and a Python
semantic layer that compiles modeled sources into SQL. This benchmark uses that
Python semantic layer directly so the generated SQL can run against the same
DuckDB data as the other local comparison layers.

## Scoring Boundary

- `q01`-`q07` use ordinary KtX sources, measures, joins, filters, and aggregate
  locality, so they are scored as `native`.
- `q08`-`q16` execute through KtX `sql:` sources or query-level filters. In
  this pack the temporal-validity, conversion-window, and aggregate-predicate
  semantics are authored as SQL/query workarounds rather than as reusable
  governed primitives in the KtX semantic model.
- Output check: KtX's rows match Semantic Rails on every question except q07
  and q16. Those two read the 11-row `comparison_order_lifecycle` view. There
  KtX agrees with Cube, Malloy and Snowflake Semantic Views, which read the same
  view, and with MetricFlow's stale committed answers. The Semantic Rails pack
  reads the full lifecycle table (see the pack README).

Run from the repository root:

```bash
PYTHONPATH=/tmp/ktx-compare/python/ktx-sl \
  uv run --with sqlglot --with pydantic --with pyyaml \
  python comparisons/semantic_layers/ktx/scripts/run_questions.py
```

Set `KTX_SL_PATH=/path/to/ktx/python/ktx-sl` to use a different KtX checkout.
