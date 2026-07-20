SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   COUNT(1) as "orders"
FROM comparison_orders as base
 LEFT JOIN comparison_customers AS customers_0
  ON customers_0."customer_id"=base."customer_id"
WHERE (customers_0."lifetime_spend_cents"*1.0/100.0::DOUBLE)>=500
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

