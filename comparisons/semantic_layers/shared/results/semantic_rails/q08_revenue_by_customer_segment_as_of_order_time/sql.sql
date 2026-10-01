WITH leaf_1 AS (
SELECT
  comparison_customer_history.customer_segment AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1
FROM comparison_orders
LEFT JOIN comparison_customer_history ON comparison_orders.customer_id = comparison_customer_history.customer_id AND comparison_customer_history.valid_from <= comparison_orders.ordered_at AND (comparison_customer_history.valid_to > comparison_orders.ordered_at OR (comparison_customer_history.valid_to IS NULL))
GROUP BY
  comparison_customer_history.customer_segment,
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
  base.g1 AS "dimension.jaffle_customer_history_segment",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS revenue_usd
FROM guarded_base AS base
ORDER BY
  revenue_usd DESC