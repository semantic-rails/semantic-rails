-- Portable: delivered revenue by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS DATE_TRUNC('month', order_lifecycle.delivered_at) AS delivered_month
  METRICS order_lifecycle.delivered_revenue
)
ORDER BY delivered_month;
