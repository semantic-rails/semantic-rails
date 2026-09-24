-- q01_orders_by_month
-- Question: How many orders did we have by month?
-- Expected semantics: Count distinct orders on the ordered timestamp at month grain.
--
-- Interpretation:
--   One row per calendar month of comparison_orders.ordered_at (naive UTC),
--   orders = COUNT(DISTINCT order_id).
--
-- Ambiguities resolved:
--   * Empty orders: 1,119 orders have no line items and a $0 subtotal/total.
--     They are still rows in the orders fact, so they are counted. Excluding them
--     is the alternative. It would lower the 12-month total from 59,652 to 58,533.
--   * No filter on store or customer. Every order row counts once
--     (order_id is unique in the view, so DISTINCT is only a guard).
--   * Months with no orders are not emitted (none exist from 2016-09 through 2017-08).
SELECT
  CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
  COUNT(DISTINCT o.order_id) AS orders
FROM comparison_orders AS o
GROUP BY 1
ORDER BY 1
