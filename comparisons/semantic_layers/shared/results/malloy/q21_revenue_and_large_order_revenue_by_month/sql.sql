SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE as "revenue_usd",
   (COALESCE(SUM(CASE WHEN base."order_total_cents">=5000 THEN base."order_total_cents" END),0)*1.0/100.0::DOUBLE) as "large_order_revenue_usd"
FROM comparison_orders as base
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

