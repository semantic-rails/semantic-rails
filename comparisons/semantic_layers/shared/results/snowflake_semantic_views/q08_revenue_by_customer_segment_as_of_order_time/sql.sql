-- Stretch: historical segment as-of order time.
-- This stays as verified SQL because the as-of validity predicate is the
-- important differentiator in this comparison pack.
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  h.customer_segment,
  SUM(o.order_total_cents / 100.0) AS revenue_usd
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
LEFT JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMER_HISTORY AS h
  ON o.customer_id = h.customer_id
 AND o.ordered_at >= h.valid_from
 AND (h.valid_to IS NULL OR o.ordered_at < h.valid_to)
GROUP BY 1, 2
ORDER BY 1, 2;
