-- Edge: same-store session conversion within 7 days.
WITH matched_sessions AS (
  SELECT
    s.session_id,
    DATE_TRUNC('month', s.started_at) AS session_month,
    MIN(o.ordered_at) AS first_ordered_at
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_STOREFRONT_SESSIONS AS s
  LEFT JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
    ON o.customer_id = s.customer_id
   AND o.store_id = s.store_id
   AND o.ordered_at >= s.started_at
   AND o.ordered_at < DATEADD(day, 7, s.started_at)
  GROUP BY 1, 2
)
SELECT
  session_month,
  COUNT_IF(first_ordered_at IS NOT NULL) * 1.0 / NULLIF(COUNT(*), 0) AS same_store_conversion_rate_7d
FROM matched_sessions
GROUP BY 1
ORDER BY 1;
