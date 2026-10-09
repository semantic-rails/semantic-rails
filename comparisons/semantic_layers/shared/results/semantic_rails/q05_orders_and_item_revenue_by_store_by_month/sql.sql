WITH leaf_1 AS (
SELECT
  comparison_stores.store_name AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT comparison_orders.order_id) AS m1
FROM comparison_orders
LEFT JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
GROUP BY
  comparison_stores.store_name,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
leaf_2 AS (
SELECT
  comparison_stores.store_name AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_order_items.item_revenue_cents / 100.0) AS m2,
  COUNT(1) AS m2_rows
FROM comparison_order_items
INNER JOIN comparison_orders ON comparison_order_items.order_id = comparison_orders.order_id
LEFT JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
GROUP BY
  comparison_stores.store_name,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
combined_2 AS (
SELECT
  COALESCE(left_side.g1, right_side.g1) AS g1,
  COALESCE(left_side.t, right_side.t) AS t,
  left_side.m1 AS m1,
  right_side.m2 AS m2,
  right_side.m2_rows AS m2_rows
FROM leaf_1 AS left_side
FULL OUTER JOIN leaf_2 AS right_side ON left_side.g1 IS NOT DISTINCT FROM right_side.g1 AND left_side.t IS NOT DISTINCT FROM right_side.t
),
coverage_1 AS (
SELECT
  MIN(DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))) AS loaded_from,
  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(comparison_orders.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(comparison_orders.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(comparison_orders.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) END) AS loaded_to
FROM comparison_orders
),
guarded_base AS (
SELECT
  base.g1 AS g1,
  base.t AS t,
  COALESCE(NULLIF(base.m1, 0), CASE WHEN MAX(base.m1) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1,
  COALESCE(base.m2, CASE WHEN COUNT(base.m2) OVER () > 0 AND ((base.m2_rows IS NULL) OR base.m2_rows = 0) THEN 0 END) AS m2
FROM combined_2 AS base
CROSS JOIN coverage_1
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS orders,
  base.m2 AS item_revenue_usd
FROM guarded_base AS base
ORDER BY
  orders DESC