{{ config(materialized='view') }}

select *
from jaffle_order
