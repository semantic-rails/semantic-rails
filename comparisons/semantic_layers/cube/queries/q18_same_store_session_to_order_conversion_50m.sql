SELECT
  month,
  1.0 * SUM(CASE WHEN converted = 1 THEN sessions ELSE 0 END) / SUM(sessions)
    AS same_store_conversion_rate_50m
FROM (
  SELECT
    DATE_TRUNC('month', started_at) AS month,
    customer_id,
    store_id,
    started_at,
    MAX(sessions) AS sessions,
    MAX(CASE WHEN ordered_at >= started_at
      AND EXTRACT(EPOCH FROM ordered_at) - EXTRACT(EPOCH FROM started_at) < 3000
      THEN 1 ELSE 0 END) AS converted
  FROM (
    SELECT
      storefront_sessions.customer_id,
      storefront_sessions.store_id,
      storefront_sessions.started_at,
      same_store_orders.ordered_at,
      MEASURE(storefront_sessions.session_starts) AS sessions
    FROM storefront_sessions
    CROSS JOIN same_store_orders
    GROUP BY 1, 2, 3, 4
  ) AS session_orders
  GROUP BY 1, 2, 3, 4
) AS session_groups
GROUP BY 1
