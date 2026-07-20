{{ config(materialized='view') }}

select *
from jaffle_store
