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
