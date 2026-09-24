-- q10_orders_from_customers_with_10plus_orders_in_month
-- Question: What was order volume from customers with more than 10 orders in the same month?
-- Expected semantics: Use an aggregate-on-aggregate predicate scoped to customer and
--   month, then count qualifying orders.
--
-- Interpretation:
--   Step 1 (inner aggregate): count each customer's orders in each calendar month of
--     ordered_at, across all stores.
--   Step 2 (predicate): a customer-month qualifies if that count is greater than 10,
--     that is 11 or more.
--   Step 3 (outer aggregate): count the orders in qualifying customer-months, by month.
--   A qualifying customer-month contributes all of its orders.
--
-- Ambiguities resolved:
--   * "More than 10" means strictly greater than 10, following the question text.
--     The title's "10 Plus" could be read as 10 or more. That alternative changes
--     every month: 380 customer-months have exactly 10 orders, and the 12-month
--     total would be 43,981 instead of 40,181.
--   * "The same month" means the calendar month (DATE_TRUNC month on naive-UTC
--     ordered_at), not a rolling 30 days.
--   * The customer-month count covers all stores. No customer orders at more than
--     one store, so this does not matter here.
--   * Empty $0 orders count toward the threshold and as qualifying orders.
--   * Months with no qualifying orders are omitted. Every month has some.
WITH orders_by_month AS (
  SELECT
    o.order_id,
    o.customer_id,
    CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month
  FROM comparison_orders AS o
),
qualifying_customer_months AS (
  SELECT
    customer_id,
    month
  FROM orders_by_month
  WHERE customer_id IS NOT NULL
  GROUP BY 1, 2
  HAVING COUNT(DISTINCT order_id) > 10
)
SELECT
  obm.month,
  COUNT(DISTINCT obm.order_id) AS qualifying_orders
FROM orders_by_month AS obm
INNER JOIN qualifying_customer_months AS q
  ON q.customer_id = obm.customer_id
 AND q.month = obm.month
GROUP BY 1
ORDER BY 1
