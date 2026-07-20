WITH leaf_1__order_count_customer_month_source_1__leaf_1 AS (
SELECT
  jaffle_order.customer_id AS g1,
  jaffle_order.store_id AS g2,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT jaffle_order.order_id) AS m1
FROM jaffle_order
GROUP BY
  jaffle_order.customer_id,
  jaffle_order.store_id,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
),
leaf_1__order_count_customer_month_source_1 AS (
SELECT
  base.g1 AS "dimension.jaffle_customer_id",
  base.g2 AS "dimension.jaffle_store_id",
  base.t AS t,
  base.m1 AS __predicate_value
FROM leaf_1__order_count_customer_month_source_1__leaf_1 AS base
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
  jaffle_store.store_name AS g1,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  SUM(jaffle_order.order_total_cents / 100.0) AS m1
FROM jaffle_order
INNER JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id
INNER JOIN leaf_1__qualified_customers_month_by_order_count_1 ON jaffle_order.customer_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_customer_id" AND jaffle_store.store_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_store_id" AND DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) = leaf_1__qualified_customers_month_by_order_count_1.t
GROUP BY
  jaffle_store.store_name,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS qualifying_revenue_usd
FROM leaf_1 AS base
ORDER BY
  t ASC,
  g1 ASC