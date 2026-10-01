WITH leaf_1 AS (
SELECT
  comparison_stores.store_name AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1
FROM comparison_orders
LEFT JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
GROUP BY
  comparison_stores.store_name,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
guarded_base AS (
SELECT
  base.g1 AS g1,
  base.t AS t,
  CASE WHEN COUNT(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1
FROM leaf_1 AS base
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS revenue_usd
FROM guarded_base AS base
ORDER BY
  revenue_usd DESC