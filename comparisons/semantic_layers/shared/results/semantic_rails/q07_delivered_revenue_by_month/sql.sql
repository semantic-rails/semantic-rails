WITH leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(jaffle_order_lifecycle.delivered_at AS TIMESTAMP)) AS t,
  SUM(jaffle_order_lifecycle.order_total_cents / 100.0) AS m1
FROM jaffle_order_lifecycle
GROUP BY
  DATE_TRUNC('month', CAST(jaffle_order_lifecycle.delivered_at AS TIMESTAMP))
)
SELECT
  base.t AS "temporal_role.jaffle_lifecycle_delivered_at__month",
  base.m1 AS delivered_revenue
FROM leaf_1 AS base
ORDER BY
  t ASC