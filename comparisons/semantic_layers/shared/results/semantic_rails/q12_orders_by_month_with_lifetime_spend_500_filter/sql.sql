WITH leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base AS (
SELECT
  jaffle_customer.customer_id AS g1,
  jaffle_customer.customer_id AS __snapshot_key_1,
  jaffle_customer.first_order_at AS __snapshot_order,
  jaffle_customer.lifetime_spend_cents / 100.0 AS __snapshot_value
FROM jaffle_customer
),
leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete AS (
SELECT
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.g1 AS g1,
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_key_1 AS __snapshot_key_1,
  MAX(leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_order) AS __snapshot_order
FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base
GROUP BY
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.g1,
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_key_1
),
leaf_1__lifetime_spend_customer_source_1__leaf_1 AS (
SELECT
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.g1 AS g1,
  SUM(leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_value) AS m1
FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base
INNER JOIN leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete ON leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.g1 IS NOT DISTINCT FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete.g1 AND leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_key_1 IS NOT DISTINCT FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete.__snapshot_key_1 AND leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_order = leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete.__snapshot_order
GROUP BY
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.g1
),
leaf_1__lifetime_spend_customer_source_1 AS (
SELECT
  base.g1 AS "dimension.jaffle_customer_id",
  base.m1 AS __predicate_value
FROM leaf_1__lifetime_spend_customer_source_1__leaf_1 AS base
),
leaf_1__qualified_customers_by_lifetime_spend_1 AS (
SELECT DISTINCT
  predicate_source."dimension.jaffle_customer_id" AS "dimension.jaffle_customer_id"
FROM leaf_1__lifetime_spend_customer_source_1 AS predicate_source
WHERE
  predicate_source.__predicate_value >= 500
),
leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT jaffle_order.order_id) AS m1
FROM jaffle_order
INNER JOIN leaf_1__qualified_customers_by_lifetime_spend_1 ON jaffle_order.customer_id = leaf_1__qualified_customers_by_lifetime_spend_1."dimension.jaffle_customer_id"
GROUP BY
  DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  base.m1 AS filtered_orders
FROM leaf_1 AS base
ORDER BY
  t ASC