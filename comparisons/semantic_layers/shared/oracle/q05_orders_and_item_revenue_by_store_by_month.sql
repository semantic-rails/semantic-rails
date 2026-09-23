-- q05_orders_and_item_revenue_by_store_by_month
-- Question: Show orders and item revenue in one query by store and month.
-- Expected semantics: Combine order-grain and item-grain metrics without silently
--   fanning out order counts.
--
-- Interpretation:
--   Aggregate each metric at its own grain, then join the two on the shared keys
--   (order month, store):
--     orders           = COUNT(DISTINCT order_id) from comparison_orders
--                        (identical to q01 split by store),
--     item_revenue_usd = SUM(item_revenue_cents) / 100.0 from comparison_order_items
--                        (identical to q03 summed over product types, split by store).
--   Items take the month and store of their parent order, which the view carries.
--
-- Ambiguities resolved:
--   * Fan-out: orders are never counted from the item join. The item view has
--     95,368 rows for 59,652 orders, so COUNT(*) over it would overstate orders.
--   * Empty orders (no items, $0) count as orders and add nothing to item revenue.
--   * A FULL OUTER JOIN keeps a store-month that has only one of the two metrics.
--     A missing metric would show as 0. On this data all 18 store-months have both.
--   * Store-months with no orders and no items are omitted.
WITH order_grain AS (
  SELECT
    CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
    o.store_id,
    COUNT(DISTINCT o.order_id) AS orders
  FROM comparison_orders AS o
  GROUP BY 1, 2
),
item_grain AS (
  SELECT
    CAST(DATE_TRUNC('month', i.ordered_at) AS DATE) AS month,
    i.store_id,
    SUM(i.item_revenue_cents) AS item_revenue_cents
  FROM comparison_order_items AS i
  GROUP BY 1, 2
)
SELECT
  COALESCE(og.month, ig.month) AS month,
  s.store_name,
  COALESCE(og.orders, 0) AS orders,
  COALESCE(ig.item_revenue_cents, 0) / 100.0 AS item_revenue_usd
FROM order_grain AS og
FULL OUTER JOIN item_grain AS ig
  ON ig.month = og.month
 AND ig.store_id = og.store_id
LEFT JOIN comparison_stores AS s
  ON s.store_id = COALESCE(og.store_id, ig.store_id)
ORDER BY 1, 2
