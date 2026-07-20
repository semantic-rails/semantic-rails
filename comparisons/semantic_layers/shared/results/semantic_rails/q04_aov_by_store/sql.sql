WITH leaf_1 AS (
SELECT
  jaffle_store.store_name AS g1,
  SUM(jaffle_order.order_total_cents / 100.0) AS m1,
  COUNT(DISTINCT jaffle_order.order_id) AS m2
FROM jaffle_order
INNER JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id
GROUP BY
  jaffle_store.store_name
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.m1 / NULLIF(base.m2, 0) AS aov_usd
FROM leaf_1 AS base
ORDER BY
  aov_usd DESC