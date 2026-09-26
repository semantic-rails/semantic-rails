# Malloy Comparison Pack

This project pins `@malloydata/cli` `0.0.57` (exact version, locked in `package-lock.json`) and runs the shared Jaffle comparison dataset through a single Malloy model, `models/jaffle.malloy`, with one named query per question.

## Setup

1. Install the locked dependencies:

   ```bash
   npm ci
   ```

2. Validate the model:

   ```bash
   npm run validate
   ```

3. Execute the comparison suite from the repository root:

   ```bash
   uv run python comparisons/semantic_layers/malloy/scripts/run_questions.py
   ```

If `node_modules` is absent, the runner installs the locked CLI with `npm ci` before compiling queries.

## Comparison Notes

All 16 questions (q01-q16) run through Malloy sources, joins, measures and named queries; the model has no `jaffle.sql(...)` blocks and reads no precomputed rollup column.

The frozen-model questions (q17-q24) run with the model unchanged: each is a query in its own file, `queries/<id>.malloy`, that imports `models/jaffle.malloy`. Filtered aggregates (q21), ad hoc aggregates over a column (q22), calculations (`sum_moving` for q19, `lag` for q20) and the model's query-derived sources (q23, q24) answer them. q17 and q18 change the conversion window, which the model's join fixes at 7 days, so their queries extend `storefront_sessions` with a join of their own (`join_many: ... on` a 14-day or 50-minute window); that is a query-level source extension, Malloy syntax rather than SQL, so the rubric labels it `native`. `sum_moving` and `lag` step over month rows, which equals the calendar rule only because every month has orders.

- q01-q07 use the baseline sources (`orders`, `order_items`, `customers`, `stores`) and `order_lifecycle` for the delivered-time clock. q06 counts the seed's `is_new_customer_order` flag, as every layer does.
- q08 and q16 join `customer_history` with `join_one: ... on` a validity-window condition (`valid_from <= clock < valid_to`, open-ended when `valid_to` is null), on the order's time for q08 and on the delivered time for q16. An order with no valid row keeps a null segment because Malloy joins are left joins.
- q09 and q15 join each storefront session to the same customer's orders placed within 7 days after it started (`join_many: ... on`). `count()` is a symmetric aggregate, so it still counts each session once when a session matches several orders. q15 is a filtered measure over the same join that requires the order's store to equal the session's store.
- q10, q13 and q14 join each order to a query-derived source (`source: x is orders -> { group_by: ...; aggregate: ... }`) that counts orders per customer-month (q10, q13) or per customer-store-month (q14), then filter to groups with more than 10 orders.
- q11 and q12 join each order to a query-derived source that computes each customer's lifetime order count and lifetime spend from `orders`, instead of reading the `lifetime_order_count` and `lifetime_spend_cents` columns of `comparison_customers`.
- No question needs a workaround in this pack at this version.
- The runner compiles each named query with the CLI (from the question's query file when it has one, otherwise from the model) and executes that SQL against the shared DuckDB database, so the SQL recorded in `sql.sql` (which the rubric reads) is exactly the SQL that ran.
- Rows within a group-by tie (for example two stores in one month) have no stated order, so their order can change between runs.
- Generated artifacts are written to `../shared/results/malloy/`.
