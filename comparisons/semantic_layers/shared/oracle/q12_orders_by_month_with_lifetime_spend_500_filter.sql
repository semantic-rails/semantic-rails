-- q12_orders_by_month_with_lifetime_spend_500_filter
-- Question: How many orders did we have by month after applying a query-time filter for
--   customers with lifetime spend of at least 500 USD?
-- Expected semantics: Apply a query-time customer-scoped predicate over lifetime spend
--   without requiring a separately authored published metric for the filtered result.
--
-- Interpretation:
--   Lifetime spend is each customer's SUM(order_total_cents) over all their orders
--   in the dataset, derived from comparison_orders. I do not use
--   lifetime_spend_cents. Keep customers with lifetime spend >= 50,000 cents ($500),
--   then count all their orders by the calendar month of ordered_at. The filter is
--   on the customer and applies to every order they placed, in any month.
--
-- Ambiguities resolved:
--   * Spend means order_total_cents including tax (what the customer paid), the
--     same basis as revenue in q02. Alternative: pre-tax subtotal. That changes the
--     answer: 603 qualifying customers instead of 611, and 50,914 orders over 12
--     months instead of 51,356.
--   * "Lifetime" means all orders in the dataset, evaluated at query time.
--     Alternative: cumulative spend up to and including each order. That changes
--     the answer a lot (24,834 orders), because early orders from big spenders
--     would be excluded.
--   * "At least" is inclusive (>=). No customer has exactly $500.00, so > gives the
--     same answer.
--   * Empty $0 orders from qualifying customers count as orders.
--   * Cross-check: the derived spend matches comparison_customers.lifetime_spend_cents
--     for all 939 customers.
--   * Months with no qualifying orders are omitted. There are none.
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
WHERE cls.lifetime_spend_cents >= 50000
GROUP BY 1
ORDER BY 1
