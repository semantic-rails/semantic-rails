WITH leaf_1 AS (
SELECT
  comparison_stores.store_name AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT comparison_orders.order_id) AS m1
FROM comparison_orders
INNER JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
GROUP BY
  comparison_stores.store_name,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
leaf_2 AS (
SELECT
  comparison_stores.store_name AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_order_items.item_revenue_cents / 100.0) AS m2
FROM comparison_order_items
INNER JOIN comparison_orders ON comparison_order_items.order_id = comparison_orders.order_id
INNER JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
GROUP BY
  comparison_stores.store_name,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
combined_2 AS (
SELECT
  COALESCE(left_side.g1, right_side.g1) AS g1,
  COALESCE(left_side.t, right_side.t) AS t,
  left_side.m1 AS m1,
  right_side.m2 AS m2
FROM leaf_1 AS left_side
FULL OUTER JOIN leaf_2 AS right_side ON left_side.g1 IS NOT DISTINCT FROM right_side.g1 AND left_side.t IS NOT DISTINCT FROM right_side.t
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS orders,
  base.m2 AS item_revenue_usd
FROM combined_2 AS base
ORDER BY
  orders DESC