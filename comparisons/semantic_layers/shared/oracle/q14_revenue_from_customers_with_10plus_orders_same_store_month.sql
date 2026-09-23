-- q14_revenue_from_customers_with_10plus_orders_same_store_month
-- Question: What was revenue by store by month from customers who placed more than 10
--   orders in that same store-month?
-- Expected semantics: Evaluate the qualifying predicate at customer plus month plus store
--   scope, then sum qualifying revenue without losing the outer grouping context.
--
-- Interpretation:
--   Step 1: count orders per (customer, store, calendar month of ordered_at).
--   Step 2: a (customer, store, month) qualifies if that count is greater than 10.
--   Step 3: sum order_total_cents / 100.0 for the orders in qualifying
--     (customer, store, month) groups, and report by store_name and month.
--   A customer's orders at another store, or in another month, never count toward
--   the threshold for this store-month.
--
-- Ambiguities resolved:
--   * "More than 10" means strictly greater than 10, the same as q10.
--     Alternative: 10 or more, from the title's "10 Plus". That changes all 18
--     store-months: the 380 customer-store-months with exactly 10 orders would
--     qualify, and the all-time total would be $429,720.17 instead of $367,056.30.
--   * Revenue means order_total_cents including tax, the same as q02.
--   * The predicate counts orders (COUNT DISTINCT order_id), not revenue, and
--     includes empty $0 orders.
--   * Data note: no customer orders at more than one store, so customer+store+month
--     scope gives the same answer as customer+month scope on this data. The query
--     still scopes by store, as asked.
--   * Store-months with no qualifying revenue are omitted, not zero-filled. All 18
--     store-months with orders have qualifying revenue here, so this does not
--     matter.
WITH order_facts AS (
  SELECT
    o.order_id,
    o.customer_id,
    o.store_id,
    o.order_total_cents,
    CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month
  FROM comparison_orders AS o
),
qualifying_customer_store_months AS (
  SELECT
    customer_id,
    store_id,
    month
  FROM order_facts
  WHERE customer_id IS NOT NULL
  GROUP BY 1, 2, 3
  HAVING COUNT(DISTINCT order_id) > 10
)
SELECT
  f.month,
  s.store_name,
  SUM(f.order_total_cents) / 100.0 AS qualifying_revenue_usd
FROM order_facts AS f
INNER JOIN qualifying_customer_store_months AS q
  ON q.customer_id = f.customer_id
 AND q.store_id = f.store_id
 AND q.month = f.month
LEFT JOIN comparison_stores AS s
  ON s.store_id = f.store_id
GROUP BY 1, 2
ORDER BY 1, 2
