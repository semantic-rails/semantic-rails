# Oracle semantics contract

This is an independent answer key for `comparisons/semantic_layers/shared/questions.yml`.
Each rule below was derived from the question text and from the raw data behind the
`comparison_*` views in `jaffle_comparison.duckdb`, which `bootstrap_shared_duckdb.py`
builds. The answers were computed and reviewed on dataset fingerprint `4f24ad8276da…`.
`run_oracle.py` records that fingerprint and a second one over these queries and
`questions.yml`, and the output check refuses an answer key whose data, queries or questions
have changed since it ran. Each `<id>.sql` has a full comment block listing its ambiguities
and alternatives.

## Shared conventions

- **Month and day**: `CAST(DATE_TRUNC('month', ts) AS DATE)` or `CAST(ts AS DATE)`.
  Timestamps are naive and used as recorded, with no time-zone conversion.
- **Clock**: `ordered_at`, unless the question names another (delivered time in q07
  and q16, session start in q09 and q15).
- **Revenue**: `order_total_cents / 100.0`, the order total including tax, as recorded.
  The seed defines revenue and lifetime spend this way, and `questions.yml` now states it.
  The strongest counter-argument: sales tax is usually not counted as revenue, and dbt's
  jaffle-shop revenue metric is pre-tax. A layer that used pre-tax `subtotal_cents`
  consistently would differ from this key by definition, not by error, on q02, q04, q07,
  q08, q12, q14 and q16.
- **Item revenue**: `item_revenue_cents / 100.0`, the product price per item row. It sums
  to `subtotal_cents` on every order.
- **Orders**: `COUNT(DISTINCT order_id)` over `comparison_orders`. This includes the
  1,119 orders that have no items and a $0 total.
- **Derived customer facts**: lifetime counts, lifetime spend, first order,
  per-month counts and 7-day conversion are computed from order and session rows.
  The precomputed rollups, flags and `converted_order_id` are used only as
  cross-checks.
- **Empty groups are omitted**, never zero-filled. The exception is the NULL segment
  group in q08 and q16, which the question requires. q05's full outer join would fill a
  missing metric with 0 for a store-month that has only one of the two; no store-month here
  lacks either.

## Per-question rules

| id | Rule |
|---|---|
| q01 | Count distinct orders per `ordered_at` month. |
| q02 | Sum revenue per (`ordered_at` month, order's store). |
| q03 | Sum item revenue per (parent order's `ordered_at` month, item `product_type`). |
| q04 | For each store over all time, total revenue / count of distinct orders (ratio of sums). Stores with no orders are omitted. |
| q05 | Orders counted from the orders grain and item revenue summed from the items grain, each aggregated separately per (month, store), then full-outer-joined. There is no fan-out, so orders equal q01 split by store. |
| q06 | Count each customer's earliest order (tie-break by `order_id`) in its `ordered_at` month. The months sum to the 939 customers. |
| q07 | Sum revenue per `delivered_at` month from `comparison_order_lifecycle`. |
| q08 | Left-join each order to the customer-history row with `valid_from <= ordered_at < valid_to` (NULL `valid_to` is open-ended). Sum revenue per (`ordered_at` month, segment). Orders with no valid row stay in the output as `customer_segment = NULL`. |
| q09 | A session converted if the same customer has an order with `started_at < ordered_at <= started_at + 7 days`. Rate = converted sessions / all sessions, per session-start month. |
| q10 | A (customer, calendar month) qualifies when it has more than 10 orders (11 or more). Count all orders in qualifying customer-months, per month. |
| q11 | A repeat customer has more than one order over the whole dataset. Count all of their orders, their first included, per (`ordered_at` month, store). |
| q12 | Keep customers whose total order spend over the whole dataset is at least 50,000 cents ($500). Count all of their orders per `ordered_at` month. |
| q13 | The q10 predicate over the full calendar month, not month-to-date. Count the qualifying orders per `ordered_at` day. |
| q14 | A (customer, store, calendar month) qualifies when it has more than 10 orders. Sum revenue of the orders in qualifying groups per (month, store). |
| q15 | The q09 rule plus `order.store_id = session.store_id`. Rate = converted sessions / all sessions, per session-start month. |
| q16 | q08 with `delivered_at` as the clock for both the month bucket and the history validity lookup. |

## Readings that change the answer on this data

- Revenue as pre-tax subtotal instead of order total: changes q02, q04, q07, q08,
  q14 and q16, and changes q12 through the spend basis.
- "10 Plus" read as 10 or more instead of "more than 10": changes every row of q10,
  q13 and q14.
- In q11, counting only non-first orders instead of all orders of repeat
  customers: 58,713 instead of 59,646.
- In q12, spend as a running total up to each order instead of lifetime: 24,834
  instead of 51,356 orders.
- In q13, a month-to-date running count instead of the full-month count: 13,901
  instead of 40,181 orders.
- In q09 and q15, using the session table's `converted_order_id` instead of the stated
  rule: 0.8 instead of 1.0.
- In q09 and q15, counting matching orders instead of converted sessions: 34 orders / 10
  sessions = 3.4 instead of 1.0.
- In q05, counting `DISTINCT order_id` through the item view (a common guard against
  fan-out) drops the 1,119 orders with no items: 58,533 instead of 59,652 orders
  (2016-09 Philadelphia: 1,339 instead of 1,367).

## Readings that do not change the answer on this data

- Half-open or closed validity intervals, and "later" as strict or inclusive.
- 7-day window inclusive or exclusive, and tie-breaking of first orders.
- Same-store matching (q15 equals q09) and store-scoped vs customer-month
  predicates (q14).
- The delivered clock vs the ordered clock: q07 equals q02 per month, and q16
  equals q08.
