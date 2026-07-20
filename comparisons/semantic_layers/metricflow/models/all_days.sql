{{ config(materialized='table') }}

with bounds as (
  select
    cast(min(ordered_at) as date) - interval 30 day as min_day,
    cast(max(ordered_at) as date) + interval 30 day as max_day
  from jaffle_order
),
series as (
  select cast(gs.date_day as date) as date_day
  from bounds,
  lateral generate_series(bounds.min_day, bounds.max_day, interval 1 day) as gs(date_day)
)
select *
from series
