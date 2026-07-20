{{ config(materialized='view') }}

with matched as (
    select
        s.session_id,
        s.customer_id,
        s.store_id,
        s.started_at,
        min(o.ordered_at) as first_ordered_at
    from jaffle_storefront_session as s
    left join jaffle_order as o
        on s.customer_id = o.customer_id
       and s.store_id = o.store_id
       and o.ordered_at >= s.started_at
       and o.ordered_at < s.started_at + interval '7 day'
    group by 1, 2, 3, 4
)

select
    session_id,
    customer_id,
    store_id,
    started_at,
    case when first_ordered_at is not null then 1 else 0 end as same_store_converted_sessions_7d
from matched
