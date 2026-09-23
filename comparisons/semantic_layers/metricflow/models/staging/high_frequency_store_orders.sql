{{ config(materialized='view') }}

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
    o.customer_id,
    o.store_id,
    o.ordered_at,
    o.order_total_cents / 100.0 as revenue_usd
from comparison_orders as o
inner join customer_store_months as c
    on o.customer_id = c.customer_id
   and o.store_id = c.store_id
   and date_trunc('month', o.ordered_at) = c.ordered_month
where c.monthly_orders > 10
