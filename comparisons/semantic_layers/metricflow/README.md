# MetricFlow Comparison Project

Pinned for this comparison:

- `dbt-metricflow==0.11.0`
- `dbt-duckdb==1.10.1`

Local setup:

```bash
uv venv .venv
uv pip install --python .venv/bin/python --prerelease=allow dbt-metricflow==0.11.0 dbt-duckdb==1.10.1
DBT_PROFILES_DIR=$(pwd) .venv/bin/dbt build
DBT_PROFILES_DIR=$(pwd) .venv/bin/mf validate-configs
DBT_PROFILES_DIR=$(pwd) .venv/bin/mf list metrics
```

Then execute the comparison suite:

```bash
uv run python comparisons/semantic_layers/metricflow/scripts/run_questions.py
```

Artifacts are written under `comparisons/semantic_layers/shared/results/metricflow/`.

The runner will bootstrap `.venv` and install the pinned packages automatically if the local environment has been cleaned.

Notes:

- The install currently requires `--prerelease=allow` because `dbt-metricflow==0.11.0` resolves through `dbt-semantic-interfaces==0.9.4.dev0`.
- `order_items.sql` is intentionally enriched with `ordered_at`, `store_id`, and `customer_id` from `jaffle_order` so time and grouping work cleanly in MetricFlow on the shared item grain.
- q09 and q10 are implemented through derived dbt views so they should be read as `precomputed` support rather than native MetricFlow semantics.
