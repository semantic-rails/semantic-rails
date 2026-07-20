WITH leaf_1__lifetime_order_count_customer_source_1__leaf_1 AS (
SELECT
  jaffle_customer.customer_id AS g1,
  SUM(jaffle_customer.lifetime_order_count) AS m1
FROM jaffle_customer
GROUP BY
  jaffle_customer.customer_id
),
leaf_1__lifetime_order_count_customer_source_1 AS (
SELECT
  base.g1 AS "dimension.jaffle_customer_id",
  base.m1 AS __predicate_value
FROM leaf_1__lifetime_order_count_customer_source_1__leaf_1 AS base
),
leaf_1__qualified_customers_by_lifetime_order_count_1 AS (
SELECT DISTINCT
  predicate_source."dimension.jaffle_customer_id" AS "dimension.jaffle_customer_id"
FROM leaf_1__lifetime_order_count_customer_source_1 AS predicate_source
WHERE
  predicate_source.__predicate_value > 1
),
leaf_1 AS (
SELECT
  jaffle_store.store_name AS g1,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT jaffle_order.order_id) AS m1
FROM jaffle_order
INNER JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id
INNER JOIN leaf_1__qualified_customers_by_lifetime_order_count_1 ON jaffle_order.customer_id = leaf_1__qualified_customers_by_lifetime_order_count_1."dimension.jaffle_customer_id"
GROUP BY
  jaffle_store.store_name,
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.g1 AS "dimension.jaffle_store_name",
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS repeat_customer_orders
FROM leaf_1 AS base
ORDER BY
  t ASC,
  g1 ASC