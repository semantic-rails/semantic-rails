SELECT 
   base."session_month" as "session_month",
   (COUNT(CASE WHEN base."converted_within_7d"=true THEN 1 END))*1.0/(COUNT(1)) as "same_store_conversion_rate_7d"
FROM (
  with matched as (
    select
      s.session_id,
      date_trunc('month', s.started_at) as session_month,
      min(o.ordered_at) as first_ordered_at
    from comparison_storefront_sessions as s
    left join comparison_orders as o
      on s.customer_id = o.customer_id
     and s.store_id = o.store_id
     and o.ordered_at >= s.started_at
     and o.ordered_at < s.started_at + interval '7 day'
    group by 1, 2
  )
  select
    session_id,
    session_month,
    first_ordered_at is not null as converted_within_7d
  from matched
) as base
GROUP BY 1
ORDER BY 1 asc NULLS LAST
LIMIT 5000

