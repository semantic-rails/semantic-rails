-- Edge: query-time-style lifetime spend filter, approximated as verified SQL.
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  COUNT(*) AS filtered_orders
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMERS AS c
  ON o.customer_id = c.customer_id
WHERE c.lifetime_spend_cents >= 50000
GROUP BY 1
ORDER BY 1;
