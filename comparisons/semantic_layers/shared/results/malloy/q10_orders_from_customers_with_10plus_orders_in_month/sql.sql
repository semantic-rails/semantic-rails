SELECT 
   base."ordered_month" as "ordered_month",
   COUNT(1) as "qualifying_orders"
FROM (
  with customer_months as (
    select
      customer_id,
      date_trunc('month', ordered_at) as ordered_month,
      count(*) as monthly_orders
    from comparison_orders
    group by 1, 2
  )
  select
    o.order_id,
    date_trunc('day', o.ordered_at) as ordered_day,
    date_trunc('month', o.ordered_at) as ordered_month
  from comparison_orders as o
  inner join customer_months as c
    on o.customer_id = c.customer_id
   and date_trunc('month', o.ordered_at) = c.ordered_month
  where c.monthly_orders > 10
) as base
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

