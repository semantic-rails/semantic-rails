WITH leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT jaffle_order.order_id) AS m1
FROM jaffle_order
GROUP BY
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS orders
FROM leaf_1 AS base
ORDER BY
  t ASC