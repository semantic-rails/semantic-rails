-- Baseline: item revenue by product type by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS order_items.product_type, DATE_TRUNC('month', order_items.ordered_at) AS ordered_month
  METRICS order_items.item_revenue_usd
)
ORDER BY ordered_month, product_type;
