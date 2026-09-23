# Examples

Worked Query IR documents for the bundled synthetic `jaffle_shop` package. Pass a file to the
CLI with `--query-json @examples/<file>`, or send its contents as the `query` field of an MCP
tool call or an `/api/v1/*` request. `tests/semantic_rails/test_examples.py` checks every file
against `schemas/query_ir.v1.json` and the runtime, so they stay current.

| File | Shows |
| --- | --- |
| `jaffle_shop_revenue_by_store.json` | Revenue by store: a measure grouped by a dimension, ordered and limited |
| `inline_yoy.json` | Monthly revenue next to the same month a year earlier, as a `prior_period` expression |
| `cross_clock_conversion.json` | A 7-day, same-store session-to-order conversion rate by session day |
| `qualified_metric_rollup.json` | Orders by store and month, counting only customers with four or more orders (a `metric_predicate` filter), top 10 rows |
| `percentile_threshold.json` | Revenue from the top decile of customers by lifetime spend, with the percentile threshold inline in one query |
| `anchored_window_cohort_retention.json` | A per-customer anchored window. **IR contract only:** it validates to a structured `INVALID_ANCHOR_ROLE` error until SQL lowering ships |

The `_note` field in a file explains its shape and any limits.

From a source checkout:

```bash
uv run semantic-rails validate --package jaffle_shop --query-json @examples/inline_yoy.json
uv run semantic-rails compile --package jaffle_shop --query-json @examples/inline_yoy.json
uv run semantic-rails query --package jaffle_shop --query-json @examples/jaffle_shop_revenue_by_store.json
```

`bi_consumer/metric_card.py` shows a small BI integration. It stores a metric's identity from
the metric portability contract and executes the governed query by reference, without
constructing SQL.

To model your own data, start with `semantic-rails init` (see the [README](../README.md)).
