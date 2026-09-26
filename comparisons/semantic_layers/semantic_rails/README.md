# Comparison Semantic Rails Package

This is a comparison-only package for the local `semantic_rails` runtime.

It intentionally reuses the shared Jaffle DuckDB bootstrap and only models the subset used in the comparison suite:

- `orders`
- `order_items`
- `customers`
- `stores`
- `customer_history`
- `order_lifecycle`
- `storefront_sessions`

Run:

```bash
uv run python comparisons/semantic_layers/shared/scripts/bootstrap_shared_duckdb.py
uv run python comparisons/semantic_layers/semantic_rails/scripts/run_questions.py
```

Captured outputs are written under `comparisons/semantic_layers/shared/results/semantic_rails/`.

The frozen-model questions (q17-q24) run with this package unchanged, through the Query API
only (`queries/q17-q24`): a `conversion` expression with a 14-day or 50-minute window (q17,
q18), a `scoped_aggregate` of revenue over orders of 50 USD or more (q21), `avg` and `max`
aggregate overrides on `item_revenue_usd` (q22), and `metric_filters` predicates with new
thresholds (q23; q24 computes each customer's lifetime spend from orders, not from the
precomputed column q12 reads). q19 and q20 need a model change: `rolling` and `prior_period` run
over a dense calendar, this package declares no calendar entity, and the engine refuses them
("time.fill requires a calendar entity in the package"). Their refused queries and validation
errors are kept in the results.
