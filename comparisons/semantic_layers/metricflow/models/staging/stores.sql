{{ config(materialized='view') }}

select *
from comparison_stores
