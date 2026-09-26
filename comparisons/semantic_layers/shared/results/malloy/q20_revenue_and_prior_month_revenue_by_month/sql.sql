SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE as "revenue_usd",
   LAG((COALESCE(SUM(base."order_total_cents"),0)*1.0/100.0::DOUBLE)) OVER(  ORDER BY  DATE_TRUNC('month', base."ordered_at") asc NULLS LAST ) as "prior_month_revenue_usd"
FROM comparison_orders as base
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

