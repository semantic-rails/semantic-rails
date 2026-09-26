-- q21_revenue_and_large_order_revenue_by_month
-- Question: What was revenue by month, and how much of it came from orders of 50 USD or more?
-- Expected semantics: For each ordered month, total revenue beside the revenue of orders
--   whose total is at least 50 USD, in one result.
--
-- Interpretation:
--   Revenue is SUM(order_total_cents) / 100.0, as in q02, with all stores combined.
--   large_order_revenue_usd sums the same column over orders with
--   order_total_cents >= 5000. Both are grouped by the calendar month of ordered_at.
--   A month with no large orders would show 0. Every month has some (from 33 orders in
--   2016-11 to 184 in 2017-07).
--
-- Ambiguities resolved:
--   * "50 USD or more" is inclusive and tested on the order total including tax, the
--     same basis as revenue. No order totals exactly 5,000 cents. The nearest are 4,992
--     below and 5,088 above, so > 5000 gives the same answer.
--   * Alternative: test the threshold on the pre-tax subtotal. That changes the answer:
--     1,052 large orders instead of 1,180, and 2016-09 would be 2,223.86 instead of
--     2,376.50. Summing the pre-tax subtotal as well changes every value.
--   * The precomputed is_large_order flag is set at 30 USD (order_total_cents >= 3000),
--     not 50 USD, and is not used. Using it changes every month: 2016-09 would be
--     6,074.75.
--   * "How much of it" is read as an amount, not a share. A share would be
--     large_order_revenue_usd / revenue_usd.
--   * Empty $0 orders count in revenue and are never large.
--   * Discrimination: revenue_usd equals q02 summed over stores. large_order_revenue_usd
--     is 7.9-13.2% of it in every month, so a layer that returned revenue in both columns
--     would not match.
SELECT
  CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
  SUM(o.order_total_cents) / 100.0 AS revenue_usd,
  COALESCE(SUM(o.order_total_cents) FILTER (WHERE o.order_total_cents >= 5000), 0) / 100.0
    AS large_order_revenue_usd
FROM comparison_orders AS o
GROUP BY 1
ORDER BY 1
