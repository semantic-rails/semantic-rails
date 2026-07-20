-- Edge: daily orders from customers with more than 10 orders in the containing month.
WITH customer_months AS (
  SELECT
    customer_id,
    DATE_TRUNC('month', ordered_at) AS ordered_month,
    COUNT(*) AS monthly_orders
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS
  GROUP BY 1, 2
)
SELECT
  DATE_TRUNC('day', o.ordered_at) AS ordered_day,
  COUNT(*) AS qualifying_orders
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN customer_months AS c
  ON o.customer_id = c.customer_id
 AND DATE_TRUNC('month', o.ordered_at) = c.ordered_month
WHERE c.monthly_orders > 10
GROUP BY 1
ORDER BY 1;
