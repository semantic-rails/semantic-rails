SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   stores_0."store_name" as "store_name",
   COUNT(1) as "orders"
FROM comparison_orders as base
 LEFT JOIN comparison_stores AS stores_0
  ON stores_0."store_id"=base."store_id"
 LEFT JOIN comparison_customers AS customers_0
  ON customers_0."customer_id"=base."customer_id"
WHERE customers_0."lifetime_order_count">1
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

