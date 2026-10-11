WITH leaf_1 AS (
SELECT
  comparison_order_items.product_type AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  AVG(comparison_order_items.item_revenue_cents / 100.0) AS m1,
  COUNT(1) AS "m1_rows",
  MAX(comparison_order_items.item_revenue_cents / 100.0) AS m2,
  COUNT(1) AS "m2_rows"
FROM comparison_order_items
INNER JOIN comparison_orders ON comparison_order_items.order_id = comparison_orders.order_id
GROUP BY
  comparison_order_items.product_type,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_item_product_type",
  base.t AS "period",
  base.m1 AS "avg_item_revenue_usd",
  base.m2 AS "max_item_revenue_usd",
  base."m1_rows" AS "m1_rows",
  base."m2_rows" AS "m2_rows"
FROM leaf_1 AS base
ORDER BY
  t ASC NULLS LAST