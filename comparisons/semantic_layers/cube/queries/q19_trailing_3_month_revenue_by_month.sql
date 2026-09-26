SELECT
  month,
  SUM(revenue_usd) OVER (
    ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
  ) AS trailing_3_month_revenue_usd
FROM (
  SELECT DATE_TRUNC('month', ordered_at) AS month, MEASURE(revenue_usd) AS revenue_usd
  FROM orders
  GROUP BY 1
) AS monthly
ORDER BY 1
