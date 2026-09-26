SELECT
  month,
  revenue_usd,
  LAG(revenue_usd) OVER (ORDER BY month) AS prior_month_revenue_usd
FROM (
  SELECT DATE_TRUNC('month', ordered_at) AS month, MEASURE(revenue_usd) AS revenue_usd
  FROM orders
  GROUP BY 1
) AS monthly
ORDER BY 1
