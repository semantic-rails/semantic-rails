{{ config(materialized='view') }}

select *
from jaffle_storefront_session
