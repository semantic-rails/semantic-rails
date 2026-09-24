{{ config(materialized='view') }}

-- comparison_order_items already carries each item's order time, store and customer.
select *
from comparison_order_items
