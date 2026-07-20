SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS stores.store_name, DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.revenue_usd
)
ORDER BY ordered_month, store_name
LIMIT 12;
