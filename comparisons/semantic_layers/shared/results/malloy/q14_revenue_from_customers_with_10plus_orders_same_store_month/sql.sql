SELECT 
   base."ordered_month" as "ordered_month",
   base."store_name" as "store_name",
   COALESCE(SUM(base."revenue_usd"),0) as "qualifying_revenue_usd"
FROM (
  with customer_store_months as (
    select
      customer_id,
      store_id,
      date_trunc('month', ordered_at) as ordered_month,
      count(*) as monthly_orders
    from comparison_orders
    group by 1, 2, 3
  )
  select
    o.order_id,
    date_trunc('month', o.ordered_at) as ordered_month,
    s.store_name,
    o.order_total_cents / 100.0 as revenue_usd
  from comparison_orders as o
  inner join customer_store_months as c
    on o.customer_id = c.customer_id
   and o.store_id = c.store_id
   and date_trunc('month', o.ordered_at) = c.ordered_month
  inner join comparison_stores as s
    on o.store_id = s.store_id
  where c.monthly_orders > 10
) as base
GROUP BY 1,2
ORDER BY 1 asc NULLS LAST
LIMIT 5000

