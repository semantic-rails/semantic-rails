WITH __stage0 AS (
  SELECT 
     base."customer_id" as "customer_id",
     base."store_id" as "store_id",
     DATE_TRUNC('month', base."ordered_at") as "ordered_month",
     (COUNT(1)) as "monthly_orders"
  FROM comparison_orders as base
  GROUP BY 1,2,3
)
SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   stores_0."store_name" as "store_name",
   COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE as "revenue_usd"
FROM comparison_orders as base
 LEFT JOIN __stage0 AS customer_store_month_orders_0
  ON ((base."customer_id"=customer_store_month_orders_0."customer_id") and (base."store_id"=customer_store_month_orders_0."store_id")) and ((DATE_TRUNC('month', base."ordered_at"))=customer_store_month_orders_0."ordered_month")
 LEFT JOIN comparison_stores AS stores_0
  ON stores_0."store_id"=base."store_id"
WHERE customer_store_month_orders_0."monthly_orders">10
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

