WITH leaf_1 AS (
SELECT
  comparison_stores.store_name AS g1,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1,
  COUNT(DISTINCT comparison_orders.order_id) AS m2
FROM comparison_orders
INNER JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
GROUP BY
  comparison_stores.store_name
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.m1 / NULLIF(base.m2, 0) AS aov_usd
FROM leaf_1 AS base
ORDER BY
  aov_usd DESC