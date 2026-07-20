SELECT 
   DATE_TRUNC('month', base."delivered_at") as "delivered_month",
   COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE as "delivered_revenue_usd"
FROM comparison_order_lifecycle as base
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

