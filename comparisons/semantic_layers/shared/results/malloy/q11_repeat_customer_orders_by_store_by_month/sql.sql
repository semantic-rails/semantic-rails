WITH __stage0 AS (
  SELECT 
     base."customer_id" as "customer_id",
     (COUNT(1)) as "lifetime_orders",
     (COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE) as "lifetime_spend_usd"
  FROM comparison_orders as base
  GROUP BY 1
)
SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   stores_0."store_name" as "store_name",
   COUNT(1) as "orders"
FROM comparison_orders as base
 LEFT JOIN __stage0 AS customer_order_facts_0
  ON customer_order_facts_0."customer_id"=base."customer_id"
 LEFT JOIN comparison_stores AS stores_0
  ON stores_0."store_id"=base."store_id"
WHERE customer_order_facts_0."lifetime_orders">1
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

