WITH leaf_1 AS (
SELECT
  comparison_order_items.product_type AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_order_items.item_revenue_cents / 100.0) AS m1,
  COUNT(1) AS m1_rows
FROM comparison_order_items
INNER JOIN comparison_orders ON comparison_order_items.order_id = comparison_orders.order_id
GROUP BY
  comparison_order_items.product_type,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
guarded_base AS (
SELECT
  base.g1 AS g1,
  base.t AS t,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.g1 AS "dimension.jaffle_item_product_type",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS item_revenue_usd
FROM guarded_base AS base
ORDER BY
  item_revenue_usd DESC