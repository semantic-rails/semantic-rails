SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   customer_history_0."customer_segment" as "customer_segment",
   COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE as "revenue_usd"
FROM comparison_orders as base
 LEFT JOIN comparison_customer_history AS customer_history_0
  ON ((base."customer_id"=customer_history_0."customer_id") and (base."ordered_at">=customer_history_0."valid_from")) and ((customer_history_0."valid_to" IS NULL or (base."ordered_at"<customer_history_0."valid_to")))
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

