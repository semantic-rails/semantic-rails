# Output Consistency

Generated at `2026-06-23T23:47:13-04:00` using `semantic_rails` as the reference layer.

- Matched: `14`
- Mismatched: `2`
- Not comparable: `0`

## q01_orders_by_month Orders By Month

- Layer statuses: `semantic_rails=native, metricflow=native, cube=native, malloy=native, snowflake_semantic_views=native, ktx=native`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q02_revenue_by_store_by_month Revenue By Store By Month

- Layer statuses: `semantic_rails=native, metricflow=native, cube=native, malloy=native, snowflake_semantic_views=native, ktx=native`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q03_item_revenue_by_product_type_by_month Item Revenue By Product Type By Month

- Layer statuses: `semantic_rails=native, metricflow=native, cube=native, malloy=native, snowflake_semantic_views=native, ktx=native`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q04_aov_by_store Average Order Value By Store

- Layer statuses: `semantic_rails=native, metricflow=native, cube=native, malloy=native, snowflake_semantic_views=native, ktx=native`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q05_orders_and_item_revenue_by_store_by_month Orders And Item Revenue In One Query

- Layer statuses: `semantic_rails=native, metricflow=native, cube=workaround, malloy=native, snowflake_semantic_views=native, ktx=native`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q06_new_customer_orders_by_month New Customer Orders By Month

- Layer statuses: `semantic_rails=native, metricflow=native, cube=native, malloy=native, snowflake_semantic_views=native, ktx=native`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q07_delivered_revenue_by_month Delivered Revenue By Month

- Layer statuses: `semantic_rails=native, metricflow=native, cube=native, malloy=native, snowflake_semantic_views=native, ktx=native`
- Comparison status: `mismatched`
- Mismatch vs semantic_rails on `metricflow`: `{"left": 12, "right": 1, "type": "row_count"}`
- Mismatch vs semantic_rails on `cube`: `{"left": 12, "right": 1, "type": "row_count"}`
- Mismatch vs semantic_rails on `malloy`: `{"left": 12, "right": 1, "type": "row_count"}`
- Mismatch vs semantic_rails on `snowflake_semantic_views`: `{"left": 12, "right": 1, "type": "row_count"}`
- Mismatch vs semantic_rails on `ktx`: `{"left": 12, "right": 1, "type": "row_count"}`

## q08_revenue_by_customer_segment_as_of_order_time Revenue By Historical Customer Segment

- Layer statuses: `semantic_rails=native, metricflow=native, cube=workaround, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q09_session_to_order_conversion_7d Session To Order Conversion Within 7 Days

- Layer statuses: `semantic_rails=native, metricflow=precomputed, cube=precomputed, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q10_orders_from_customers_with_10plus_orders_in_month Orders From Customers With 10 Plus Orders In Month

- Layer statuses: `semantic_rails=native, metricflow=precomputed, cube=precomputed, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q11_repeat_customer_orders_by_store_by_month Repeat Customer Orders By Store By Month

- Layer statuses: `semantic_rails=native, metricflow=precomputed, cube=workaround, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q12_orders_by_month_with_lifetime_spend_500_filter Orders By Month With Lifetime Spend Filter

- Layer statuses: `semantic_rails=native, metricflow=precomputed, cube=workaround, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q13_daily_orders_from_customers_with_10plus_orders_in_month Daily Orders From Customers With 10 Plus Orders In Month

- Layer statuses: `semantic_rails=native, metricflow=precomputed, cube=precomputed, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q14_revenue_from_customers_with_10plus_orders_same_store_month Revenue From Customers With 10 Plus Orders In Same Store Month

- Layer statuses: `semantic_rails=native, metricflow=precomputed, cube=precomputed, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q15_same_store_session_to_order_conversion_7d Same Store Session To Order Conversion Within 7 Days

- Layer statuses: `semantic_rails=native, metricflow=precomputed, cube=precomputed, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `matched`
- Comparable layers: `semantic_rails, metricflow, cube, malloy, snowflake_semantic_views, ktx`

## q16_revenue_by_customer_segment_as_of_delivered_time Revenue By Historical Customer Segment As Of Delivered Time

- Layer statuses: `semantic_rails=native, metricflow=native, cube=precomputed, malloy=workaround, snowflake_semantic_views=workaround, ktx=workaround`
- Comparison status: `mismatched`
- Mismatch vs semantic_rails on `metricflow`: `{"left": 32, "right": 2, "type": "row_count"}`
- Mismatch vs semantic_rails on `cube`: `{"left": 32, "right": 2, "type": "row_count"}`
- Mismatch vs semantic_rails on `malloy`: `{"left": 32, "right": 2, "type": "row_count"}`
- Mismatch vs semantic_rails on `snowflake_semantic_views`: `{"left": 32, "right": 2, "type": "row_count"}`
- Mismatch vs semantic_rails on `ktx`: `{"left": 32, "right": 2, "type": "row_count"}`
