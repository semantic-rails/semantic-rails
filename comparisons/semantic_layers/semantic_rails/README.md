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
q18), `rolling` and `prior_period` expressions over monthly revenue (q19, q20), a
`scoped_aggregate` of revenue over orders of 50 USD or more (q21), `avg` and `max`
aggregate overrides on `item_revenue_usd` (q22), and customer predicates with new
thresholds (q23, q24). The engine supplies a bounded calendar for q19 and q20 when
the package declares none. Their executed queries, compiled SQL and answers are kept in the
results; both match the independent answer key on the shared dataset.

q11 and q12 count the `orders` metric where a customer predicate over the whole dataset
(`over: all_time`) holds, computed from orders at query time, as q24 does: more than one order
(q11) or at least 500 USD of revenue (q12). They don't read the customers view's precomputed
`lifetime_order_count` and `lifetime_spend_cents` columns, which the package's
`repeat_customer_orders` and `lifetime_spend` metrics are defined on; until 2026-10-27 they did,
and the rubric labeled them `precomputed`.
