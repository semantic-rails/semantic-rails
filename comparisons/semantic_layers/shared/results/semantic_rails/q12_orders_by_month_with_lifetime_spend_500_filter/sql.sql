WITH leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base AS (
SELECT
  comparison_customers.customer_id AS g1,
  comparison_customers.customer_id AS __snapshot_key_1,
  comparison_customers.lifetime_spend_cents / 100.0 AS __snapshot_value,
  comparison_customers.first_order_at AS __snapshot_order
FROM comparison_customers
WHERE
  comparison_customers.first_order_at IS NOT NULL
),
leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete AS (
SELECT
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_key_1 AS __snapshot_key_1,
  MAX(leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_order) AS __snapshot_order
FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base
GROUP BY
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_key_1
),
leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_rows AS (
SELECT
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.g1 AS g1,
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_key_1 AS __snapshot_key_1,
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_value AS __snapshot_value
FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base
INNER JOIN leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete ON leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_key_1 IS NOT DISTINCT FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete.__snapshot_key_1 AND leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_base.__snapshot_order = leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_complete.__snapshot_order
),
leaf_1__lifetime_spend_customer_source_1__leaf_1 AS (
SELECT
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_rows.g1 AS g1,
  SUM(leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_rows.__snapshot_value) AS m1
FROM leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_rows
GROUP BY
  leaf_1__lifetime_spend_customer_source_1__leaf_1__snapshot_rows.g1
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
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  COUNT(DISTINCT comparison_orders.order_id) AS m1
FROM comparison_orders
INNER JOIN leaf_1__qualified_customers_by_lifetime_spend_1 ON comparison_orders.customer_id = leaf_1__qualified_customers_by_lifetime_spend_1."dimension.jaffle_customer_id"
GROUP BY
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
observed_1_rows AS (
SELECT
  1 AS seen
FROM comparison_orders
WHERE
  comparison_orders.order_id IS NOT NULL
LIMIT 1
),
observed_1 AS (
SELECT
  COUNT(observed_1_rows.seen) AS seen
FROM observed_1_rows
),
guarded_base AS (
SELECT
  base.t AS t,
  CASE WHEN MAX(base.m1) OVER () > 0 OR observed_1.seen > 0 THEN COALESCE(base.m1, 0) END AS m1
FROM leaf_1 AS base
CROSS JOIN observed_1
)
SELECT
  base.t AS "period",
  base.m1 AS "filtered_orders"
FROM guarded_base AS base
ORDER BY
  t ASC NULLS LAST