WITH leaf_1 AS (
SELECT
  jaffle_customer_history.customer_segment AS g1,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  SUM(jaffle_order.order_total_cents / 100.0) AS m1
FROM jaffle_order
LEFT JOIN jaffle_customer_history ON jaffle_order.customer_id = jaffle_customer_history.customer_id AND jaffle_customer_history.valid_from <= jaffle_order.ordered_at AND (jaffle_customer_history.valid_to > jaffle_order.ordered_at OR (jaffle_customer_history.valid_to IS NULL))
GROUP BY
  jaffle_customer_history.customer_segment,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_customer_history_segment",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS revenue_usd
FROM leaf_1 AS base
ORDER BY
  revenue_usd DESC