-- Baseline: orders by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.orders
)
ORDER BY ordered_month;
