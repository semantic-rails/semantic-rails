-- q23_orders_from_customers_with_5plus_orders_in_month
-- Question: What was order volume from customers with more than 5 orders in the same month?
-- Expected semantics: The q10 rule with the threshold at more than 5 orders in the
--   customer-month instead of more than 10.
--
-- Interpretation:
--   Step 1 (inner aggregate): count each customer's orders in each calendar month of
--     ordered_at, across all stores.
--   Step 2 (predicate): a customer-month qualifies if that count is greater than 5,
--     that is 6 or more.
--   Step 3 (outer aggregate): count the orders in qualifying customer-months, by month.
--   A qualifying customer-month contributes all of its orders.
--
-- Ambiguities resolved:
--   * "More than 5" means strictly greater than 5, following the question text. The
--     title's "5 Plus" could be read as 5 or more. That alternative changes every
--     month: 299 customer-months have exactly 5 orders, and the 12-month total would be
--     57,029 instead of 55,534.
--   * The predicate uses the full calendar month, not month-to-date. Counting only
--     orders after the customer's running count in the month passes 5 gives 32,914.
--   * "The same month" means the calendar month of naive ordered_at, not a rolling 30
--     days. The count covers all stores. No customer orders at more than one store, so
--     a store-scoped count gives the same answer.
--   * Empty $0 orders count toward the threshold and as qualifying orders.
--   * Months with no qualifying orders are omitted. Every month has some.
--   * Discrimination: this differs from q10 in every month (2016-09: 1,145 instead of
--     755; 55,534 instead of 40,181 in total).
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
  HAVING COUNT(DISTINCT order_id) > 5
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
