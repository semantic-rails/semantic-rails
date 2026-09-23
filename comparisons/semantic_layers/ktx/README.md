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
- Output check: KtX's rows match the independent answer key
  (`../shared/oracle/`) on all 16 questions.

Run from the repository root, with KtX checked out at the recorded commit:

```bash
test -d /tmp/ktx-compare || git clone https://github.com/Kaelio/ktx /tmp/ktx-compare
git -C /tmp/ktx-compare checkout a155c0b
PYTHONPATH=/tmp/ktx-compare/python/ktx-sl \
  uv run --with sqlglot==30.19.0 --with pydantic==2.13.4 --with pyyaml==6.0.3 \
  python comparisons/semantic_layers/ktx/scripts/run_questions.py
```

The runner records the KtX commit and the package versions in its `summary.json`.

Set `KTX_SL_PATH=/path/to/ktx/python/ktx-sl` to use a different KtX checkout.
