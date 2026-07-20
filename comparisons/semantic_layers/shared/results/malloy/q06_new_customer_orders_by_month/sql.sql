SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   COUNT(CASE WHEN base."is_new_customer_order"=true THEN 1 END) as "new_customer_orders"
FROM comparison_orders as base
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

