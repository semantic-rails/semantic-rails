WITH leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1
FROM comparison_orders
GROUP BY
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
leaf_2__revenue_usd_order_month_source_1__leaf_1 AS (
SELECT
  comparison_orders.order_id AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1
FROM comparison_orders
GROUP BY
  comparison_orders.order_id,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
leaf_2__revenue_usd_order_month_source_1__guarded_base AS (
SELECT
  base.g1 AS g1,
  base.t AS t,
  CASE WHEN COUNT(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1
FROM leaf_2__revenue_usd_order_month_source_1__leaf_1 AS base
),
leaf_2__revenue_usd_order_month_source_1 AS (
SELECT
  base.g1 AS "dimension.jaffle_order_id",
  base.t AS t,
  base.m1 AS __predicate_value
FROM leaf_2__revenue_usd_order_month_source_1__guarded_base AS base
),
leaf_2__qualified_orders_month_by_revenue_usd_1 AS (
SELECT DISTINCT
  predicate_source."dimension.jaffle_order_id" AS "dimension.jaffle_order_id",
  predicate_source.t AS t
FROM leaf_2__revenue_usd_order_month_source_1 AS predicate_source
WHERE
  predicate_source.__predicate_value >= 50
),
leaf_2 AS (
SELECT
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_orders.order_total_cents / 100.0) AS m2
FROM comparison_orders
INNER JOIN leaf_2__qualified_orders_month_by_revenue_usd_1 ON comparison_orders.order_id = leaf_2__qualified_orders_month_by_revenue_usd_1."dimension.jaffle_order_id" AND DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) = leaf_2__qualified_orders_month_by_revenue_usd_1.t
GROUP BY
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
combined_2 AS (
SELECT
  COALESCE(left_side.t, right_side.t) AS t,
  left_side.m1 AS m1,
  right_side.m2 AS m2
FROM leaf_1 AS left_side
FULL OUTER JOIN leaf_2 AS right_side ON left_side.t IS NOT DISTINCT FROM right_side.t
),
guarded_base AS (
SELECT
  base.t AS t,
  CASE WHEN COUNT(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1,
  CASE WHEN COUNT(base.m2) OVER () > 0 THEN COALESCE(base.m2, 0) END AS m2
FROM combined_2 AS base
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS revenue_usd,
  base.m2 AS large_order_revenue_usd
FROM guarded_base AS base
ORDER BY
  t ASC