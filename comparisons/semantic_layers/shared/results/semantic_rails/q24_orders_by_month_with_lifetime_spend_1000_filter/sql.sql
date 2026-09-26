WITH leaf_1__revenue_usd_customer_source_1__leaf_1 AS (
SELECT
  comparison_orders.customer_id AS g1,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1
FROM comparison_orders
GROUP BY
  comparison_orders.customer_id
),
leaf_1__revenue_usd_customer_source_1 AS (
SELECT
  base.g1 AS "dimension.jaffle_customer_id",
  base.m1 AS __predicate_value
FROM leaf_1__revenue_usd_customer_source_1__leaf_1 AS base
),
leaf_1__qualified_customers_by_revenue_usd_1 AS (
SELECT DISTINCT
  predicate_source."dimension.jaffle_customer_id" AS "dimension.jaffle_customer_id"
FROM leaf_1__revenue_usd_customer_source_1 AS predicate_source
WHERE
  predicate_source.__predicate_value >= 1000
),
leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT comparison_orders.order_id) AS m1
FROM comparison_orders
INNER JOIN leaf_1__qualified_customers_by_revenue_usd_1 ON comparison_orders.customer_id = leaf_1__qualified_customers_by_revenue_usd_1."dimension.jaffle_customer_id"
GROUP BY
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS filtered_orders
FROM leaf_1 AS base
ORDER BY
  t ASC