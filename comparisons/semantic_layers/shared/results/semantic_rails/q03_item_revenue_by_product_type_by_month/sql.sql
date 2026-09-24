WITH leaf_1 AS (
SELECT
  comparison_order_items.product_type AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_order_items.item_revenue_cents / 100.0) AS m1
FROM comparison_order_items
INNER JOIN comparison_orders ON comparison_order_items.order_id = comparison_orders.order_id
GROUP BY
  comparison_order_items.product_type,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_item_product_type",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS item_revenue_usd
FROM leaf_1 AS base
ORDER BY
  item_revenue_usd DESC