{{ config(materialized='view') }}

select
    o.order_id,
    o.customer_id,
    o.store_id,
    o.ordered_at
from jaffle_order as o
inner join jaffle_customer as c
    on o.customer_id = c.customer_id
where c.lifetime_spend_cents >= 50000
