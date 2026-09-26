SELECT
  month,
  SUM(order_revenue_usd) AS revenue_usd,
  SUM(CASE WHEN order_revenue_usd >= 50 THEN order_revenue_usd ELSE 0 END) AS large_order_revenue_usd
FROM (
  SELECT DATE_TRUNC('month', ordered_at) AS month, revenue_usd AS order_revenue_usd
  FROM orders
) AS order_rows
GROUP BY 1
ORDER BY 1
