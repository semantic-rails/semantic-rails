WITH leaf_1 AS (
SELECT
  jaffle_item.product_type AS g1,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  SUM(jaffle_item.item_revenue_cents / 100.0) AS m1
FROM jaffle_item
INNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id
GROUP BY
  jaffle_item.product_type,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_item_product_type",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS item_revenue_usd
FROM leaf_1 AS base
ORDER BY
  item_revenue_usd DESC