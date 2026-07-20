WITH leaf_1 AS (
SELECT
  jaffle_customer_history.customer_segment AS g1,
  DATE_TRUNC('month', CAST(jaffle_order_lifecycle.delivered_at AS TIMESTAMP)) AS t,
  SUM(jaffle_order_lifecycle.order_total_cents / 100.0) AS m1
FROM jaffle_order_lifecycle
LEFT JOIN jaffle_customer_history ON jaffle_order_lifecycle.customer_id = jaffle_customer_history.customer_id AND jaffle_customer_history.valid_from <= jaffle_order_lifecycle.delivered_at AND (jaffle_customer_history.valid_to > jaffle_order_lifecycle.delivered_at OR (jaffle_customer_history.valid_to IS NULL))
GROUP BY
  jaffle_customer_history.customer_segment,
  DATE_TRUNC('month', CAST(jaffle_order_lifecycle.delivered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_customer_history_segment",
  base.t AS "temporal_role.jaffle_lifecycle_delivered_at__month",
  base.m1 AS delivered_revenue
FROM leaf_1 AS base
ORDER BY
  t ASC,
  g1 ASC