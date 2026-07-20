SELECT 
   stores_0."store_name" as "store_name",
   AVG(base."order_total_cents")*1.0/100.0::DOUBLE as "aov_usd"
FROM comparison_orders as base
 LEFT JOIN comparison_stores AS stores_0
  ON stores_0."store_id"=base."store_id"
GROUP BY 1
ORDER BY 2 desc NULLS LAST
LIMIT 5000

