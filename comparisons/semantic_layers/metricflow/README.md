# MetricFlow Comparison Project

Pinned for this comparison in `requirements.in`, and locked with hashes in
`requirements.lock`:

- `dbt-metricflow==0.11.0`
- `dbt-duckdb==1.10.1`

Local setup:

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements.lock
DBT_PROFILES_DIR=$(pwd) .venv/bin/dbt build
DBT_PROFILES_DIR=$(pwd) .venv/bin/mf validate-configs
DBT_PROFILES_DIR=$(pwd) .venv/bin/mf list metrics
```

Then execute the comparison suite:

```bash
uv run python comparisons/semantic_layers/metricflow/scripts/run_questions.py
```

Artifacts are written under `comparisons/semantic_layers/shared/results/metricflow/`.

The runner creates `.venv` if it is missing, syncs it to `requirements.lock` on every run, and records the installed versions in `summary.json`.

Notes:

- `dbt-metricflow==0.11.0` requires the pre-release `dbt-semantic-interfaces==0.9.4.dev0`. `requirements.in` names it, so the lock resolves with `--prerelease=if-necessary-or-explicit` and every other package stays on a release. (`--prerelease=allow` pulled a DuckDB nightly.)
- `order_items.sql` is intentionally enriched with `ordered_at`, `store_id`, and `customer_id` from `jaffle_order` so time and grouping work cleanly in MetricFlow on the shared item grain.
- q09-q15 are implemented through helper dbt views, so they are labeled `precomputed` rather than native MetricFlow semantics. MetricFlow's native conversion metrics and metric filters are not modeled yet.
