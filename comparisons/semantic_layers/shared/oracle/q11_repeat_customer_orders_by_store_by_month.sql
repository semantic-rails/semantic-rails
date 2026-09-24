-- q11_repeat_customer_orders_by_store_by_month
-- Question: How many repeat customer orders did we have by store by month?
-- Expected semantics: Count orders from customers whose lifetime order count is greater
--   than one, while preserving outer store and month grouping.
--
-- Interpretation:
--   A customer is a repeat customer if they have more than one order in the whole
--   dataset. Lifetime means all orders, all stores, all time, counted from
--   comparison_orders. I do not use lifetime_order_count or customer_type. Every
--   order from a repeat customer counts, including that customer's first order.
--   Orders are grouped by the order's own store and ordered month.
--
-- Ambiguities resolved:
--   * The classification is at the customer level, not the order level.
--     Alternative: count only orders that are not the customer's first (a repeat
--     order as of order time). That excludes the 933 first orders of repeat
--     customers: 58,713 instead of 59,646 across all store-months.
--   * "Lifetime" is evaluated at query time over the whole dataset, not as of each
--     order.
--   * The lifetime count is not restricted to the outer store or month. The
--     customer-level predicate is computed once and then applied at store-month
--     grain. No customer orders at more than one store, so a per-store lifetime
--     would give the same answer.
--   * Only 6 of 939 customers have exactly one order, so this is almost all
--     orders.
--   * Cross-check: the derived lifetime counts match
--     comparison_customers.lifetime_order_count and customer_type for all 939
--     customers.
--   * Store-months with no repeat-customer orders are omitted. There are none.
WITH customer_lifetime AS (
  SELECT
    o.customer_id,
    COUNT(DISTINCT o.order_id) AS lifetime_orders
  FROM comparison_orders AS o
  WHERE o.customer_id IS NOT NULL
  GROUP BY 1
)
SELECT
  CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
  s.store_name,
  COUNT(DISTINCT o.order_id) AS repeat_customer_orders
FROM comparison_orders AS o
INNER JOIN customer_lifetime AS cl
  ON cl.customer_id = o.customer_id
LEFT JOIN comparison_stores AS s
  ON s.store_id = o.store_id
WHERE cl.lifetime_orders > 1
GROUP BY 1, 2
ORDER BY 1, 2
