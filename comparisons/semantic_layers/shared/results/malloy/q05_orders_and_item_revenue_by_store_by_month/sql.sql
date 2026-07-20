SELECT 
   DATE_TRUNC('month', base."ordered_at") as "ordered_month",
   stores_0."store_name" as "store_name",
   COUNT(DISTINCT base."order_id") as "orders",
   COALESCE(SUM(order_items_0."item_revenue_cents"),0)*1.0/100.0::DOUBLE as "item_revenue_usd"
FROM comparison_orders as base
 LEFT JOIN comparison_stores AS stores_0
  ON stores_0."store_id"=base."store_id"
 LEFT JOIN comparison_order_items AS order_items_0
  ON base."order_id"=order_items_0."order_id"
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

