WITH __stage0 AS (
  SELECT 
     base."customer_id" as "customer_id",
     DATE_TRUNC('month', base."ordered_at") as "ordered_month",
     (COUNT(1)) as "monthly_orders"
  FROM comparison_orders as base
  GROUP BY 1,2
)
SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   COUNT(1) as "orders"
FROM comparison_orders as base
 LEFT JOIN __stage0 AS customer_month_orders_0
  ON (base."customer_id"=customer_month_orders_0."customer_id") and ((DATE_TRUNC('month', base."ordered_at"))=customer_month_orders_0."ordered_month")
WHERE customer_month_orders_0."monthly_orders">10
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

