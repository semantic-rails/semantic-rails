-- Edge: repeat customer orders by store by month.
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  s.store_name,
  COUNT(*) AS repeat_customer_orders
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMERS AS c
  ON o.customer_id = c.customer_id
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_STORES AS s
  ON o.store_id = s.store_id
WHERE c.lifetime_order_count > 1
GROUP BY 1, 2
ORDER BY 1, 2;
