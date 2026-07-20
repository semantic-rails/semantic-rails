{{ config(materialized='view') }}

select *
from jaffle_customer_history
