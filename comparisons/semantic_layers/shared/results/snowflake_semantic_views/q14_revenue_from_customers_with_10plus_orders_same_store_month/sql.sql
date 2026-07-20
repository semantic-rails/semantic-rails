-- Edge: same-store contextual customer-month revenue.
WITH customer_store_months AS (
  SELECT
    customer_id,
    store_id,
    DATE_TRUNC('month', ordered_at) AS ordered_month,
    COUNT(*) AS monthly_orders
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS
  GROUP BY 1, 2, 3
)
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  s.store_name,
  SUM(o.order_total_cents / 100.0) AS qualifying_revenue_usd
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN customer_store_months AS c
  ON o.customer_id = c.customer_id
 AND o.store_id = c.store_id
 AND DATE_TRUNC('month', o.ordered_at) = c.ordered_month
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_STORES AS s
  ON o.store_id = s.store_id
WHERE c.monthly_orders > 10
GROUP BY 1, 2
ORDER BY 1, 2;
