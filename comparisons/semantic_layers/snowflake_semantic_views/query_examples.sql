-- Verify the YAML without creating a production object.
CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(
  'ANALYTICS.SEMANTIC_COMPARISON',
  $$<contents of jaffle_semantic_view.yaml>$$,
  TRUE
);

-- q01_orders_by_month
-- Baseline: orders by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.orders
)
ORDER BY ordered_month;

-- q02_revenue_by_store_by_month
-- Baseline: revenue by store by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS stores.store_name, DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.revenue_usd
)
ORDER BY ordered_month, store_name;

-- q03_item_revenue_by_product_type_by_month
-- Baseline: item revenue by product type by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS order_items.product_type, DATE_TRUNC('month', order_items.ordered_at) AS ordered_month
  METRICS order_items.item_revenue_usd
)
ORDER BY ordered_month, product_type;

-- q04_aov_by_store
-- Baseline: average order value by store.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS stores.store_name
  METRICS aov_usd
)
ORDER BY aov_usd DESC;

-- q05_orders_and_item_revenue_by_store_by_month
-- Portable: orders and item revenue in one query.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS stores.store_name, DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.orders, order_items.item_revenue_usd
)
ORDER BY ordered_month, store_name;

-- q06_new_customer_orders_by_month
-- Portable: new customer orders by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.new_customer_orders
)
ORDER BY ordered_month;

-- q07_delivered_revenue_by_month
-- Portable: delivered revenue by month.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS DATE_TRUNC('month', order_lifecycle.delivered_at) AS delivered_month
  METRICS order_lifecycle.delivered_revenue
)
ORDER BY delivered_month;

-- q08_revenue_by_customer_segment_as_of_order_time
-- Stretch: historical segment as-of order time.
-- This stays as verified SQL because the as-of validity predicate is the
-- important differentiator in this comparison pack.
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  h.customer_segment,
  SUM(o.order_total_cents / 100.0) AS revenue_usd
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
LEFT JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMER_HISTORY AS h
  ON o.customer_id = h.customer_id
 AND o.ordered_at >= h.valid_from
 AND (h.valid_to IS NULL OR o.ordered_at < h.valid_to)
GROUP BY 1, 2
ORDER BY 1, 2;

-- q09_session_to_order_conversion_7d
-- Stretch: session conversion within 7 days.
WITH matched_sessions AS (
  SELECT
    s.session_id,
    DATE_TRUNC('month', s.started_at) AS session_month,
    MIN(o.ordered_at) AS first_ordered_at
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_STOREFRONT_SESSIONS AS s
  LEFT JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
    ON o.customer_id = s.customer_id
   AND o.ordered_at >= s.started_at
   AND o.ordered_at < DATEADD(day, 7, s.started_at)
  GROUP BY 1, 2
)
SELECT
  session_month,
  COUNT_IF(first_ordered_at IS NOT NULL) * 1.0 / NULLIF(COUNT(*), 0) AS conversion_rate_7d
FROM matched_sessions
GROUP BY 1
ORDER BY 1;

-- q10_orders_from_customers_with_10plus_orders_in_month
-- Stretch: orders from customers with more than 10 orders in the same month.
WITH customer_months AS (
  SELECT
    customer_id,
    DATE_TRUNC('month', ordered_at) AS ordered_month,
    COUNT(*) AS monthly_orders
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS
  GROUP BY 1, 2
)
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  COUNT(*) AS qualifying_orders
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN customer_months AS c
  ON o.customer_id = c.customer_id
 AND DATE_TRUNC('month', o.ordered_at) = c.ordered_month
WHERE c.monthly_orders > 10
GROUP BY 1
ORDER BY 1;

-- q11_repeat_customer_orders_by_store_by_month
-- Edge: repeat customer orders by store by month.
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  s.store_name,
  COUNT(*) AS repeat_customer_orders
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMERS AS c
  ON o.customer_id = c.customer_id
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_STORES AS s
  ON o.store_id = s.store_id
WHERE c.lifetime_order_count > 1
GROUP BY 1, 2
ORDER BY 1, 2;

-- q12_orders_by_month_with_lifetime_spend_500_filter
-- Edge: query-time-style lifetime spend filter, approximated as verified SQL.
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  COUNT(*) AS filtered_orders
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMERS AS c
  ON o.customer_id = c.customer_id
WHERE c.lifetime_spend_cents >= 50000
GROUP BY 1
ORDER BY 1;

-- q13_daily_orders_from_customers_with_10plus_orders_in_month
-- Edge: daily orders from customers with more than 10 orders in the containing month.
WITH customer_months AS (
  SELECT
    customer_id,
    DATE_TRUNC('month', ordered_at) AS ordered_month,
    COUNT(*) AS monthly_orders
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS
  GROUP BY 1, 2
)
SELECT
  DATE_TRUNC('day', o.ordered_at) AS ordered_day,
  COUNT(*) AS qualifying_orders
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN customer_months AS c
  ON o.customer_id = c.customer_id
 AND DATE_TRUNC('month', o.ordered_at) = c.ordered_month
WHERE c.monthly_orders > 10
GROUP BY 1
ORDER BY 1;

-- q14_revenue_from_customers_with_10plus_orders_same_store_month
-- Edge: same-store contextual customer-month revenue.
WITH customer_store_months AS (
  SELECT
    customer_id,
    store_id,
    DATE_TRUNC('month', ordered_at) AS ordered_month,
    COUNT(*) AS monthly_orders
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS
  GROUP BY 1, 2, 3
)
SELECT
  DATE_TRUNC('month', o.ordered_at) AS ordered_month,
  s.store_name,
  SUM(o.order_total_cents / 100.0) AS qualifying_revenue_usd
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
INNER JOIN customer_store_months AS c
  ON o.customer_id = c.customer_id
 AND o.store_id = c.store_id
 AND DATE_TRUNC('month', o.ordered_at) = c.ordered_month
INNER JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_STORES AS s
  ON o.store_id = s.store_id
WHERE c.monthly_orders > 10
GROUP BY 1, 2
ORDER BY 1, 2;

-- q15_same_store_session_to_order_conversion_7d
-- Edge: same-store session conversion within 7 days.
WITH matched_sessions AS (
  SELECT
    s.session_id,
    DATE_TRUNC('month', s.started_at) AS session_month,
    MIN(o.ordered_at) AS first_ordered_at
  FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_STOREFRONT_SESSIONS AS s
  LEFT JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDERS AS o
    ON o.customer_id = s.customer_id
   AND o.store_id = s.store_id
   AND o.ordered_at >= s.started_at
   AND o.ordered_at < DATEADD(day, 7, s.started_at)
  GROUP BY 1, 2
)
SELECT
  session_month,
  COUNT_IF(first_ordered_at IS NOT NULL) * 1.0 / NULLIF(COUNT(*), 0) AS same_store_conversion_rate_7d
FROM matched_sessions
GROUP BY 1
ORDER BY 1;

-- q16_revenue_by_customer_segment_as_of_delivered_time
-- Edge: delivered-time historical segment slicing.
SELECT
  DATE_TRUNC('month', l.delivered_at) AS delivered_month,
  h.customer_segment,
  SUM(l.order_total_cents / 100.0) AS delivered_revenue
FROM ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_ORDER_LIFECYCLE AS l
LEFT JOIN ANALYTICS.SEMANTIC_COMPARISON.COMPARISON_CUSTOMER_HISTORY AS h
  ON l.customer_id = h.customer_id
 AND l.delivered_at >= h.valid_from
 AND (h.valid_to IS NULL OR l.delivered_at < h.valid_to)
GROUP BY 1, 2
ORDER BY 1, 2;
