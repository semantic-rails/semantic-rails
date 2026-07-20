SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   stores_0."store_name" as "store_name",
   COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE as "revenue_usd"
FROM comparison_orders as base
 LEFT JOIN comparison_stores AS stores_0
  ON stores_0."store_id"=base."store_id"
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

