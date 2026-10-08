WITH leaf_1 AS (
SELECT
  comparison_stores.store_name AS g1,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1,
  COUNT(1) AS m1_rows,
  COUNT(DISTINCT comparison_orders.order_id) AS m2
FROM comparison_orders
LEFT JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
GROUP BY
  comparison_stores.store_name
),
guarded_base AS (
SELECT
  base.g1 AS g1,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1,
  CASE WHEN MAX(base.m2) OVER () > 0 THEN COALESCE(base.m2, 0) END AS m2
FROM leaf_1 AS base
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.m1 / NULLIF(base.m2, 0) AS aov_usd
FROM guarded_base AS base
ORDER BY
  aov_usd DESC