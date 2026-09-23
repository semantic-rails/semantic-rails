-- q13_daily_orders_from_customers_with_10plus_orders_in_month
-- Question: What was the daily order volume from customers who placed more than 10 orders
--   in the containing month?
-- Expected semantics: Evaluate the predicate at customer plus month scope, then return
--   the qualifying orders at day grain.
--
-- Interpretation:
--   The predicate is the same as q10: a customer-month qualifies if the customer
--   has more than 10 orders (11 or more) in that calendar month, over the whole
--   month. Each qualifying order is then counted on its own ordered day,
--   CAST(ordered_at AS DATE). The "containing month" of an order is the calendar
--   month of its ordered_at. The daily rows sum to q10's monthly values.
--
-- Ambiguities resolved:
--   * "More than 10" means strictly greater than 10. Alternative: 10 or more, from
--     the title. That changes the answer (43,981 orders in total instead of 40,181).
--   * The predicate uses the full calendar month, not month-to-date. An order on
--     day 3 qualifies if the customer ends the month with 11 or more orders.
--     Alternative: only orders placed after the customer's running count in the
--     month passes 10. That gives 13,901 orders instead of 40,181.
--   * Day is the naive-UTC calendar date of ordered_at.
--   * Days with no qualifying orders are omitted, not zero-filled. On this data all
--     365 order days have qualifying orders.
WITH orders_by_month AS (
  SELECT
    o.order_id,
    o.customer_id,
    CAST(o.ordered_at AS DATE) AS day,
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
  obm.day,
  COUNT(DISTINCT obm.order_id) AS qualifying_orders
FROM orders_by_month AS obm
INNER JOIN qualifying_customer_months AS q
  ON q.customer_id = obm.customer_id
 AND q.month = obm.month
GROUP BY 1
ORDER BY 1
