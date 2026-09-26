WITH leaf_1__order_count_customer_month_source_1__leaf_1 AS (
SELECT
  comparison_orders.customer_id AS g1,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT comparison_orders.order_id) AS m1
FROM comparison_orders
GROUP BY
  comparison_orders.customer_id,
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
leaf_1__order_count_customer_month_source_1 AS (
SELECT
  base.g1 AS "dimension.jaffle_customer_id",
  base.t AS t,
  base.m1 AS __predicate_value
FROM leaf_1__order_count_customer_month_source_1__leaf_1 AS base
),
leaf_1__qualified_customers_month_by_order_count_1 AS (
SELECT DISTINCT
  predicate_source."dimension.jaffle_customer_id" AS "dimension.jaffle_customer_id",
  predicate_source.t AS t
FROM leaf_1__order_count_customer_month_source_1 AS predicate_source
WHERE
  predicate_source.__predicate_value > 5
),
leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT comparison_orders.order_id) AS m1
FROM comparison_orders
INNER JOIN leaf_1__qualified_customers_month_by_order_count_1 ON comparison_orders.customer_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_customer_id" AND DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) = leaf_1__qualified_customers_month_by_order_count_1.t
GROUP BY
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS qualifying_orders
FROM leaf_1 AS base
ORDER BY
  t ASC