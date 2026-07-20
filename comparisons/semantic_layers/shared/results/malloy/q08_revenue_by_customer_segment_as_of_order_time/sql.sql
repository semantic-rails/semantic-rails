SELECT 
   base."ordered_month" as "ordered_month",
   base."customer_segment" as "customer_segment",
   COALESCE(SUM(base."revenue_usd"),0) as "revenue_total_usd"
FROM (
  select
    o.order_id,
    o.ordered_at,
    date_trunc('month', o.ordered_at) as ordered_month,
    s.store_name,
    h.customer_segment,
    o.order_total_cents / 100.0 as revenue_usd
  from comparison_orders as o
  left join comparison_stores as s
    on o.store_id = s.store_id
  left join comparison_customer_history as h
    on o.customer_id = h.customer_id
   and o.ordered_at >= h.valid_from
   and (h.valid_to is null or o.ordered_at < h.valid_to)
) as base
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

