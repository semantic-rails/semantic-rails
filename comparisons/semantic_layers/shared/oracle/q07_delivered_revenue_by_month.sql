-- q07_delivered_revenue_by_month
-- Question: What was delivered revenue by month using delivered time instead of ordered time?
-- Expected semantics: Sum order revenue anchored to delivered timestamp, not ordered timestamp.
--
-- Interpretation:
--   delivered_revenue = SUM(order_total_cents) / 100.0 in USD, grouped by the
--   calendar month of comparison_order_lifecycle.delivered_at. The lifecycle view
--   has one row per order, and its order_total_cents, customer_id and store_id
--   match comparison_orders on every order.
--
-- Ambiguities resolved:
--   * Revenue means order_total_cents including tax, the same as q02.
--     Alternative: pre-tax subtotal_cents.
--   * "Delivered revenue" means revenue of delivered orders, bucketed by delivery
--     time. An order with a NULL delivered_at (undelivered) would be excluded.
--     There are none: all 59,652 orders have a delivered_at.
--   * The column is named delivered_revenue with no _usd suffix, but the value is
--     still US dollars, following the money convention.
--   * Data note: seed_jaffle.sql synthesizes delivered_at for 59,641 of the
--     59,652 orders as ordered_at + 27..49 minutes. Only 11 orders have authored
--     events. No order is delivered on a different day, let alone month, than it
--     was ordered. So on this data the result equals q02 summed over stores per
--     month: the delivered clock is used but does not change the answer.
--   * Months with no deliveries are omitted. None exist.
SELECT
  CAST(DATE_TRUNC('month', l.delivered_at) AS DATE) AS month,
  SUM(l.order_total_cents) / 100.0 AS delivered_revenue
FROM comparison_order_lifecycle AS l
WHERE l.delivered_at IS NOT NULL
GROUP BY 1
ORDER BY 1
