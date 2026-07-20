WITH leaf_1 AS (
SELECT
  jaffle_store.store_name AS g1,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  SUM(jaffle_order.order_total_cents / 100.0) AS m1
FROM jaffle_order
INNER JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id
GROUP BY
  jaffle_store.store_name,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS revenue_usd
FROM leaf_1 AS base
ORDER BY
  revenue_usd DESC