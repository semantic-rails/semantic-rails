WITH leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT CASE WHEN comparison_orders.is_new_customer_order = TRUE THEN comparison_orders.order_id ELSE NULL END) AS m1
FROM comparison_orders
GROUP BY
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS new_customer_orders
FROM leaf_1 AS base
ORDER BY
  t ASC