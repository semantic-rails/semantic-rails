WITH leaf_1 AS (
SELECT
  comparison_customer_history.customer_segment AS g1,
  DATE_TRUNC('month', CAST(comparison_order_lifecycle.delivered_at AS TIMESTAMP)) AS t,
  SUM(comparison_order_lifecycle.order_total_cents / 100.0) AS m1,
  COUNT(1) AS m1_rows
FROM comparison_order_lifecycle
LEFT JOIN comparison_customer_history ON comparison_order_lifecycle.customer_id = comparison_customer_history.customer_id AND comparison_customer_history.valid_from <= comparison_order_lifecycle.delivered_at AND (comparison_customer_history.valid_to > comparison_order_lifecycle.delivered_at OR (comparison_customer_history.valid_to IS NULL))
GROUP BY
  comparison_customer_history.customer_segment,
  DATE_TRUNC('month', CAST(comparison_order_lifecycle.delivered_at AS TIMESTAMP))
),
guarded_base AS (
SELECT
  base.g1 AS g1,
  base.t AS t,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1
FROM leaf_1 AS base
)
SELECT
  base.g1 AS "dimension.jaffle_customer_history_segment",
  base.t AS "temporal_role.jaffle_lifecycle_delivered_at__month",
  base.m1 AS delivered_revenue
FROM guarded_base AS base
ORDER BY
  t ASC NULLS LAST,
  g1 ASC NULLS LAST