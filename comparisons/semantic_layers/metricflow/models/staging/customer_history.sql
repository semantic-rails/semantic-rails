{{ config(materialized='view') }}

select *
from comparison_customer_history
