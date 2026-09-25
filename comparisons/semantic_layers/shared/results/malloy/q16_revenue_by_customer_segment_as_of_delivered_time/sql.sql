SELECT 
   DATE_TRUNC('month', base."delivered_at") as "delivered_month",
   customer_history_0."customer_segment" as "customer_segment",
   COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE as "delivered_revenue_usd"
FROM comparison_order_lifecycle as base
 LEFT JOIN comparison_customer_history AS customer_history_0
  ON ((base."customer_id"=customer_history_0."customer_id") and (base."delivered_at">=customer_history_0."valid_from")) and ((customer_history_0."valid_to" IS NULL or (base."delivered_at"<customer_history_0."valid_to")))
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

