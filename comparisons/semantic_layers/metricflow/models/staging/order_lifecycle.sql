{{ config(materialized='view') }}

select *
from jaffle_order_lifecycle
