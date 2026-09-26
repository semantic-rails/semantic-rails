SELECT
  DATE_TRUNC('month', ordered_at) AS month,
  product_type,
  AVG(item_revenue_usd) AS avg_item_revenue_usd,
  MAX(item_revenue_usd) AS max_item_revenue_usd
FROM order_items
GROUP BY 1, 2
ORDER BY 1, 2
