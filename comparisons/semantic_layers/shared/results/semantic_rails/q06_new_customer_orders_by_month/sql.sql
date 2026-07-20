WITH leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT CASE WHEN jaffle_order.is_new_customer_order = TRUE THEN jaffle_order.order_id ELSE NULL END) AS m1
FROM jaffle_order
GROUP BY
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS new_customer_orders
FROM leaf_1 AS base
ORDER BY
  t ASC