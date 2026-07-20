WITH leaf_1 AS (
SELECT
  jaffle_store.store_name AS g1,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT jaffle_order.order_id) AS m1
FROM jaffle_order
INNER JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id
GROUP BY
  jaffle_store.store_name,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
),
leaf_2 AS (
SELECT
  jaffle_store.store_name AS g1,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  SUM(jaffle_item.item_revenue_cents / 100.0) AS m2
FROM jaffle_item
INNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id
INNER JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id
GROUP BY
  jaffle_store.store_name,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
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