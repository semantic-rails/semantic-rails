# MetricFlow Comparison Project

Pinned for this comparison in `requirements.in`, and locked with hashes in
`requirements.lock` (no pre-releases):

- `dbt-metricflow==0.15.0`, which pins `metricflow==0.213.0`
- `dbt-duckdb==1.11.0`
- `dbt-core==1.12.5`, the latest release that `dbt-metricflow` accepts (`<1.13`)

`metricflow` 0.213.0 ships its own semantic interfaces (`metricflow_semantic_interfaces`), so
`dbt-semantic-interfaces` is no longer installed.

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

## Approach

The models use dbt 1.12's latest Semantic Layer YAML spec: each dbt model declares its
`semantic_model`, entities and dimensions sit on its columns, and simple metrics replace
measures. Every dbt model except the `all_days` time spine is a passthrough of one shared
`comparison_*` view, so no question depends on hand-written SQL, and none reads the
precomputed customer rollups.

- q01-q07: simple and ratio metrics. q07 aggregates on `delivered_at`.
- q08 and q16: `customer_history` is a validity-windowed (SCD Type II) semantic model, joined
  as of the order's ordered or delivered time.
- q09 and q15: conversion metrics (`session_order_conversions_7d`, and
  `same_store_session_order_conversions_7d` with `constant_properties` on `store`), base
  `session_starts`, conversion `orders`, entity `customer`, 7-day window. A conversion metric
  counts conversion events: each order is credited to the latest session before it, and the
  documented `conversion_rate` divides orders by sessions (3.4 here, since 10 sessions are
  followed by 34 orders). The question asks for the share of sessions that converted, so
  `converted_sessions_7d` counts the sessions credited with at least one order, through a
  metric filter `{{ Metric('session_order_conversions_7d', group_by=['session']) }} > 0`,
  and a ratio metric divides it by `session_starts`.
- q10, q13 and q14: metric filters over `orders`. A metric filter groups by exactly one
  entity (dbt docs, "Metrics as dimensions with metric filters"), so `orders` declares
  surrogate entities `customer_month` and `customer_store_month` by combining columns, as the
  dbt docs show under "Combine columns with a key". Each is a row-level expression over the
  order's own columns.
- q11: `repeat_customer_orders` filters on `{{ Metric('orders', group_by=['customer']) }} > 1`,
  computed from orders.
- q12: a query-time filter on the `orders` metric,
  `--where '{{ Metric("revenue_usd", group_by=["customer"]) }} >= 500'`.

The rubric (`../shared/rubric.md`) labels all 16 answers (q01-q16) `native`.

The frozen-model questions (q17-q24) run with these models unchanged, through `mf query` only.
q23 and q24 change q10's and q12's thresholds, and a query-time `--where` metric filter over an
existing entity expresses each: `{{ Metric("orders", group_by=["customer_month"]) }} > 5` and
`{{ Metric("revenue_usd", group_by=["customer"]) }} >= 1000`. The other six need a model change,
because the parameter they change is part of a metric's definition: a conversion metric's window
(q17, q18), a cumulative metric's window (q19), a derived metric's `offset_window` (q20), a
metric's own `filter:` (q21: `--where` filters every metric in the query, so it can't return
large-order revenue beside total revenue) and a simple metric's `agg` (q22).
`../shared/frozen_model.yml` gives each reason with its dbt documentation link.

## Caveats

- Conversion attribution is last-touch. If a customer had two sessions within 7 days before
  one order, only the later session is credited, although the stated rule converts both. No
  customer here has more than one session.
- The conversion window's boundaries differ from the stated rule. The executed SQL
  (`../shared/results/metricflow/q09_session_to_order_conversion_7d/sql.txt`) counts an order when
  `session_minute <= order_minute` and `session_minute > order_minute - 7 days`: the window
  [session minute, + 7 days) at minute grain. The rule is `started_at < ordered_at <= started_at
  + 7 days`. So an order in the session's own minute counts for MetricFlow but not under the rule,
  and an order exactly 7 days later counts under the rule but not for MetricFlow. No order in this
  data falls on either boundary, so q09 and q15 matching the answer key doesn't test them.
- Event timestamps (`ordered_at`, `started_at`, `delivered_at`) are declared at `minute`, their
  grain in the data, so the conversion window and the as-of joins compare exact times, not
  days. `mf validate-configs` then warns that a time spine at or below minute grain is
  recommended; the pack keeps the day spine, since no question queries below day grain.
- `dbt_project.yml` sets the behavior flag `require_nested_cumulative_type_params: false`.
  dbt-core 1.12.5 also copies a latest-spec conversion metric's `window` into the legacy
  `type_params.window`, which the flag's default rejects as an un-nested cumulative window.
- Conversion-metric inputs are `agg: sum` with `expr: "1"`. MetricFlow 0.213.0 rewrites
  `agg: count` to a sum before validating, and then rejects it as a conversion input.
- `order_items.sql` passes `comparison_order_items` through unchanged. That shared view, which every layer reads, adds each item's `ordered_at`, `store_id` and `customer_id`, so time and grouping work on the item grain.
