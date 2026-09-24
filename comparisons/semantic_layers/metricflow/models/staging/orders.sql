{{ config(materialized='view') }}

select *
from comparison_orders
