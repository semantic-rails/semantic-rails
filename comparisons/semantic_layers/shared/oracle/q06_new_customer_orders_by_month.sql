-- q06_new_customer_orders_by_month
-- Question: How many new customer orders did we have by month?
-- Expected semantics: Count only first orders, using the ordered timestamp at month grain.
--
-- Interpretation:
--   A "new customer order" is a customer's first order: the earliest ordered_at among
--   all that customer's orders in the dataset. I derive it from the orders with
--   ROW_NUMBER() and do not use customer_order_number, is_new_customer_order or
--   first_order_at. Count these orders by the calendar month of their ordered_at.
--   Each customer has at most one first order, so the months sum to the number of
--   customers with an order (939).
--
-- Ambiguities resolved:
--   * "First" means the first order in the data. The data starts on 2016-09-01, so
--     a customer's earliest order here is treated as their first ever. No earlier
--     history exists to contradict that.
--   * Ties (two orders by one customer at the same earliest instant): break by
--     order_id so exactly one order counts. Alternative: count every tied order.
--     There are no ties on this data, so the choice does not matter.
--   * Empty $0 orders can be first orders. They are orders.
--   * An order with a NULL customer_id cannot be a new-customer order. None exist.
--   * Not the alternative reading "orders from customers who ordered only once"
--     (customer_type = 'new'). The expected semantics say first orders.
--   * Cross-check: this matches comparison_orders.is_new_customer_order on all
--     59,652 orders.
WITH ranked_orders AS (
  SELECT
    o.order_id,
    o.customer_id,
    o.ordered_at,
    ROW_NUMBER() OVER (
      PARTITION BY o.customer_id
      ORDER BY o.ordered_at, o.order_id
    ) AS customer_order_seq
  FROM comparison_orders AS o
  WHERE o.customer_id IS NOT NULL
)
SELECT
  CAST(DATE_TRUNC('month', r.ordered_at) AS DATE) AS month,
  COUNT(DISTINCT r.order_id) AS new_customer_orders
FROM ranked_orders AS r
WHERE r.customer_order_seq = 1
GROUP BY 1
ORDER BY 1
