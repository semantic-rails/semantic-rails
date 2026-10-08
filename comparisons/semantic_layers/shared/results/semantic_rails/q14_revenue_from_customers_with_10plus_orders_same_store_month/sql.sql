WITH leaf_1__order_count_customer_month_source_1__leaf_1 AS (
SELECT
  comparison_orders.customer_id AS g1,
  comparison_orders.store_id AS g2,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT comparison_orders.order_id) AS m1
FROM comparison_orders
GROUP BY
  comparison_orders.customer_id,
  comparison_orders.store_id,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
leaf_1__order_count_customer_month_source_1__guarded_base AS (
SELECT
  base.g1 AS g1,
  base.g2 AS g2,
  base.t AS t,
  CASE WHEN MAX(base.m1) OVER () > 0 THEN COALESCE(base.m1, 0) END AS m1
FROM leaf_1__order_count_customer_month_source_1__leaf_1 AS base
),
leaf_1__order_count_customer_month_source_1 AS (
SELECT
  base.g1 AS "dimension.jaffle_customer_id",
  base.g2 AS "dimension.jaffle_store_id",
  base.t AS t,
  base.m1 AS __predicate_value
FROM leaf_1__order_count_customer_month_source_1__guarded_base AS base
),
leaf_1__qualified_customers_month_by_order_count_1 AS (
SELECT DISTINCT
  predicate_source."dimension.jaffle_customer_id" AS "dimension.jaffle_customer_id",
  predicate_source."dimension.jaffle_store_id" AS "dimension.jaffle_store_id",
  predicate_source.t AS t
FROM leaf_1__order_count_customer_month_source_1 AS predicate_source
WHERE
  predicate_source.__predicate_value > 10
),
leaf_1 AS (
SELECT
  comparison_stores.store_name AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1,
  COUNT(1) AS m1_rows
FROM comparison_orders
LEFT JOIN comparison_stores ON comparison_orders.store_id = comparison_stores.store_id
INNER JOIN leaf_1__qualified_customers_month_by_order_count_1 ON comparison_orders.customer_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_customer_id" AND comparison_stores.store_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_store_id" AND DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) = leaf_1__qualified_customers_month_by_order_count_1.t
GROUP BY
  comparison_stores.store_name,
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
  base.g1 AS "dimension.jaffle_store_name",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS qualifying_revenue_usd
FROM guarded_base AS base
ORDER BY
  t ASC,
  g1 ASC