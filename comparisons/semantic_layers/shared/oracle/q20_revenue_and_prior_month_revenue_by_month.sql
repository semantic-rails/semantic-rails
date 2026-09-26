-- q20_revenue_and_prior_month_revenue_by_month
-- Question: What was revenue by month, beside the revenue of the month before?
-- Expected semantics: For each ordered month, revenue and the revenue of the preceding
--   calendar month, side by side in one row.
--
-- Interpretation:
--   Revenue is SUM(order_total_cents) / 100.0, as in q02, with all stores combined.
--   For each calendar month M of ordered_at that has orders: revenue_usd is M's
--   revenue, and prior_month_revenue_usd is the revenue of calendar month M-1. One row
--   per month that has orders.
--
-- Ambiguities resolved:
--   * The first month has no prior month in the data, so its prior-month revenue is
--     NULL (not recorded), not 0. Alternative: 0. That changes one cell, 2016-09.
--   * The prior month is the calendar month before, not the previous row. A month with
--     no orders would make the next month's prior value NULL rather than the revenue
--     two months back. The months 2016-09 to 2017-08 have no gaps, so a row-based lag
--     gives the same answer.
--   * Revenue includes tax (order_total_cents). Pre-tax subtotal changes every value.
--   * Discrimination: revenue_usd equals q02 summed over stores. prior_month_revenue_usd
--     differs from it in every row (for example 2017-03: 73,950.32 beside 43,358.12),
--     so a layer that returned monthly revenue in both columns would not match.
WITH monthly_revenue AS (
  SELECT
    CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
    SUM(o.order_total_cents) AS revenue_cents
  FROM comparison_orders AS o
  GROUP BY 1
)
SELECT
  m.month,
  m.revenue_cents / 100.0 AS revenue_usd,
  p.revenue_cents / 100.0 AS prior_month_revenue_usd
FROM monthly_revenue AS m
LEFT JOIN monthly_revenue AS p
  ON p.month = CAST(m.month - INTERVAL 1 MONTH AS DATE)
ORDER BY 1
