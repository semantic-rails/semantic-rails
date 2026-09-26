-- q24_orders_by_month_with_lifetime_spend_1000_filter
-- Question: How many orders did we have by month from customers with lifetime spend of at
--   least 1000 USD?
-- Expected semantics: The q12 rule with the threshold at 1000 USD instead of 500 USD.
--
-- Interpretation:
--   Lifetime spend is each customer's SUM(order_total_cents) over all their orders in
--   the dataset, derived from comparison_orders. I do not use lifetime_spend_cents.
--   Keep customers with lifetime spend >= 100,000 cents ($1,000), then count all their
--   orders by the calendar month of ordered_at. The filter is on the customer and
--   applies to every order they placed, in any month.
--
-- Ambiguities resolved:
--   * Spend means order_total_cents including tax, as in q12. Alternative: pre-tax
--     subtotal. That changes the answer: 267 qualifying customers instead of 287, and
--     25,848 orders over 12 months instead of 27,743.
--   * "Lifetime" means all orders in the dataset, evaluated at query time.
--     Alternative: cumulative spend up to and including each order. That changes the
--     answer a lot (8,004 orders).
--   * "At least" is inclusive (>=). No customer has exactly $1,000.00 (the nearest are
--     $997.46 and $1,000.59), so > gives the same answer.
--   * Empty $0 orders from qualifying customers count as orders.
--   * Cross-check: the derived spend matches comparison_customers.lifetime_spend_cents
--     for all 939 customers.
--   * Months with no qualifying orders are omitted. There are none.
--   * Discrimination: this differs from q12 in every month (2016-09: 1,243 instead of
--     1,355; 27,743 instead of 51,356 in total).
WITH customer_lifetime_spend AS (
  SELECT
    o.customer_id,
    SUM(o.order_total_cents) AS lifetime_spend_cents
  FROM comparison_orders AS o
  WHERE o.customer_id IS NOT NULL
  GROUP BY 1
)
SELECT
  CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
  COUNT(DISTINCT o.order_id) AS filtered_orders
FROM comparison_orders AS o
INNER JOIN customer_lifetime_spend AS cls
  ON cls.customer_id = o.customer_id
WHERE cls.lifetime_spend_cents >= 100000
GROUP BY 1
ORDER BY 1
