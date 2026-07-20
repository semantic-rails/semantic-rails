-- Edge: delivered-time historical segment slicing.
SELECT
  DATE_TRUNC('month', l.delivered_at) AS delivered_month,
  h.customer_segment,
  SUM(l.order_total_cents / 100.0) AS delivered_revenue
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDER_LIFECYCLE AS l
LEFT JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMER_HISTORY AS h
  ON l.customer_id = h.customer_id
 AND l.delivered_at >= h.valid_from
 AND (h.valid_to IS NULL OR l.delivered_at < h.valid_to)
GROUP BY 1, 2
ORDER BY 1, 2;
