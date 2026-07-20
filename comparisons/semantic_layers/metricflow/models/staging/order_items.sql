{{ config(materialized='view') }}

select
  i.*,
  o.ordered_at,
  o.store_id,
  o.customer_id
from jaffle_item as i
inner join jaffle_order as o
  on i.order_id = o.order_id
