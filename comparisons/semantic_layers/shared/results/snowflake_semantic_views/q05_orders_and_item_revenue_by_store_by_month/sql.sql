-- Portable: orders and item revenue in one query.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS stores.store_name, DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.orders, order_items.item_revenue_usd
)
ORDER BY ordered_month, store_name;
