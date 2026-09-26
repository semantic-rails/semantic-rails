SELECT 
   DATE_TRUNC('month', base."started_at") as "session_month",
   (COUNT(DISTINCT CASE WHEN orders_within_50m_0."store_id"=base."store_id" THEN base."session_id" END))*1.0/(COUNT(DISTINCT base."session_id")) as "same_store_conversion_rate_50m"
FROM comparison_storefront_sessions as base
 LEFT JOIN comparison_orders AS orders_within_50m_0
  ON ((base."customer_id"=orders_within_50m_0."customer_id") and (orders_within_50m_0."ordered_at">=base."started_at")) and (orders_within_50m_0."ordered_at"<((base."started_at" + INTERVAL (50) minute)))
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

