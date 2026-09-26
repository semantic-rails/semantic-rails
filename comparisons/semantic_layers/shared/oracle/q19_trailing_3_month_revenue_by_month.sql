-- q19_trailing_3_month_revenue_by_month
-- Question: What was revenue over the trailing 3 months, by month?
-- Expected semantics: For each ordered month, sum the revenue of orders placed in that
--   calendar month and the two calendar months before it.
--
-- Interpretation:
--   Revenue is SUM(order_total_cents) / 100.0, as in q02. For each calendar month M of
--   ordered_at that has orders, trailing_3_month_revenue_usd is the revenue of orders
--   whose ordered month is M-2, M-1 or M. The months are a calendar range, not the
--   previous rows, and all stores are combined. One row per month that has orders.
--
-- Ambiguities resolved:
--   * The window includes the current month. Alternative: the three months before it
--     (M-3 to M-1). That changes every row, for example 2016-12 would be 67,894.05
--     instead of 85,518.37.
--   * Months before the data contribute nothing, so the first two rows are partial
--     windows: 2016-09 is its own revenue (18,053.28) and 2016-10 is two months
--     (39,977.23). Alternative: NULL where the window is incomplete. That changes those
--     two rows only.
--   * Calendar months, not a trailing 90 days. A 90-day window ending on the last day of
--     the month changes 8 of 12 rows (2016-11 would be 67,413.89 instead of 67,894.05).
--   * A calendar range, not the previous two rows. The months 2016-09 to 2017-08 have
--     no gaps, so a row-based window gives the same answer.
--   * A sum, not a 3-month average. An average would be a third of each value.
--   * Revenue includes tax (order_total_cents). Pre-tax subtotal changes every row
--     (2017-08 would be 286,458.00 instead of 300,607.65).
--   * Discrimination: q02 is by store and month and is not cumulative. Only 2016-09,
--     whose window holds one month, equals q02 summed over stores. Every later row is
--     larger, so a layer that returned plain monthly revenue would not match.
WITH monthly_revenue AS (
  SELECT
    CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
    SUM(o.order_total_cents) AS revenue_cents
  FROM comparison_orders AS o
  GROUP BY 1
)
SELECT
  m.month,
  SUM(w.revenue_cents) / 100.0 AS trailing_3_month_revenue_usd
FROM monthly_revenue AS m
INNER JOIN monthly_revenue AS w
  ON w.month >= CAST(m.month - INTERVAL 2 MONTH AS DATE)
 AND w.month <= m.month
GROUP BY 1
ORDER BY 1
