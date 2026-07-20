SELECT 
   base."delivered_month" as "delivered_month",
   base."customer_segment" as "customer_segment",
   COALESCE(SUM(base."delivered_revenue_usd"),0) as "delivered_revenue_total_usd"
FROM (
  select
    l.order_id,
    date_trunc('month', l.delivered_at) as delivered_month,
    h.customer_segment,
    l.order_total_cents / 100.0 as delivered_revenue_usd
  from comparison_order_lifecycle as l
  left join comparison_customer_history as h
    on l.customer_id = h.customer_id
   and l.delivered_at >= h.valid_from
   and (h.valid_to is null or l.delivered_at < h.valid_to)
) as base
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

