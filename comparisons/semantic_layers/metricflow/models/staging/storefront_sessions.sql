{{ config(materialized='view') }}

select *
from comparison_storefront_sessions
