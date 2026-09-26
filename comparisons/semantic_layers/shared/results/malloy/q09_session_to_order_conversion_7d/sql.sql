SELECT 
   DATE_TRUNC('month', base."started_at") as "session_month",
   (COUNT(DISTINCT CASE WHEN orders_within_7d_0."order_id" IS NOT NULL THEN base."session_id" END))*1.0/(COUNT(DISTINCT base."session_id")) as "conversion_rate_7d"
FROM comparison_storefront_sessions as base
 LEFT JOIN comparison_orders AS orders_within_7d_0
  ON ((base."customer_id"=orders_within_7d_0."customer_id") and (orders_within_7d_0."ordered_at">base."started_at")) and (orders_within_7d_0."ordered_at"<=((base."started_at" + INTERVAL (7) day)))
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

