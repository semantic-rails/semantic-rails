SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   base."product_type" as "product_type",
   AVG(base."item_revenue_cents")*1.0/100.0::DOUBLE as "avg_item_revenue_usd",
   max(base."item_revenue_cents")*1.0/100.0::DOUBLE as "max_item_revenue_usd"
FROM comparison_order_items as base
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

