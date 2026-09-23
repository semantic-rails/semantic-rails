-- q02_revenue_by_store_by_month
-- Question: What was revenue by store by month?
-- Expected semantics: Sum order revenue on ordered time, grouped by store and month.
--
-- Interpretation:
--   revenue_usd = SUM(order_total_cents) / 100.0 over orders, grouped by the
--   calendar month of ordered_at and the order's store_name.
--
-- Ambiguities resolved:
--   * What "order revenue" means. I use order_total_cents, the amount the customer
--     paid including tax. Three reasons: the raw column is the order's total, the
--     seed's own order-grain rollup defines revenue_cents = SUM(order_total_cents),
--     and the question set separates "revenue" (order grain) from "item revenue"
--     (item grain, which equals the pre-tax subtotal exactly on every order).
--     Alternative: pre-tax subtotal_cents. That changes every value. The all-time
--     total is $745,893.03 with tax vs $708,402.00 pre-tax.
--   * The raw data has order_total != subtotal + tax_paid by +/-1 cent on 5,562
--     orders (a rounding artifact). I sum the recorded order_total_cents and do not
--     recompute it.
--   * Store comes from the order's store_id, joined to comparison_stores for the
--     name. It is a LEFT JOIN so an order with an unknown store would still show,
--     with a NULL name. There are none.
--   * Store-months with no orders are omitted, not zero-filled. Brooklyn has rows
--     only from 2017-03 (it opened 2017-03-12). Chicago, San Francisco and
--     New Orleans have no orders in the data, so they have no rows.
SELECT
  CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
  s.store_name,
  SUM(o.order_total_cents) / 100.0 AS revenue_usd
FROM comparison_orders AS o
LEFT JOIN comparison_stores AS s
  ON s.store_id = o.store_id
GROUP BY 1, 2
ORDER BY 1, 2
