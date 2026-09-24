-- q03_item_revenue_by_product_type_by_month
-- Question: What was item revenue by product type by month?
-- Expected semantics: Sum item-grain revenue grouped by product type and ordered month.
--
-- Interpretation:
--   item_revenue_usd = SUM(item_revenue_cents) / 100.0 over order-item rows,
--   grouped by the item's product_type ('jaffle' or 'beverage') and the calendar
--   month of the parent order's ordered_at.
--
-- Ambiguities resolved:
--   * Item revenue is the product's list price per item row (each raw_items row
--     is one unit). Tax and cost are not included. Summed per order, it equals
--     subtotal_cents exactly on every order.
--   * The month is the parent order's ordered_at, which the view carries as
--     ordered_at. Items have no timestamp of their own.
--   * Orders with no items contribute nothing. No items have a NULL product_type
--     and no items are orphaned (the view inner-joins items to orders, and all
--     95,368 raw items survive).
--   * Month and product-type pairs with no items are omitted. None are missing.
SELECT
  CAST(DATE_TRUNC('month', i.ordered_at) AS DATE) AS month,
  i.product_type,
  SUM(i.item_revenue_cents) / 100.0 AS item_revenue_usd
FROM comparison_order_items AS i
GROUP BY 1, 2
ORDER BY 1, 2
