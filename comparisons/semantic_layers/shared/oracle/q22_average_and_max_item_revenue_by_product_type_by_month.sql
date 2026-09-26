-- q22_average_and_max_item_revenue_by_product_type_by_month
-- Question: What were the average and the largest item revenue per item, by product type
--   by month?
-- Expected semantics: The q03 grouping, with the average and the maximum of item revenue
--   over item rows instead of the sum.
--
-- Interpretation:
--   Over order-item rows, grouped as in q03 by the calendar month of the parent order's
--   ordered_at and the item's product_type:
--     avg_item_revenue_usd = AVG(item_revenue_cents) / 100.0
--     max_item_revenue_usd = MAX(item_revenue_cents) / 100.0
--   Each item row is one unit at the product's list price, so "per item" is per row.
--
-- Ambiguities resolved:
--   * The average is over item rows, weighted by how often each product sold.
--     Alternative: item revenue per order (the type's item revenue in an order, averaged
--     over orders containing that type). That changes every row: 2016-09 jaffle would be
--     18.7553 instead of 11.9505. Alternative: the average of the catalog prices of the
--     type (5.60 for beverages, 12.00 for jaffles). That changes every row slightly:
--     2016-09 beverage would be 5.60 instead of 5.6175.
--   * Orders with no items contribute nothing. No item has a NULL price or product type.
--   * Month and product-type pairs with no items are omitted. None are missing.
--   * Data note: every product sells in every month, so the maximum is the type's top
--     catalog price in every row (7.00 for beverages, 14.00 for jaffles). A maximum
--     taken over all months, or read from the product catalog, would match this column.
--   * Discrimination: q03 sums item revenue, so both columns differ from it in every row.
WITH item_rows AS (
  SELECT
    CAST(DATE_TRUNC('month', i.ordered_at) AS DATE) AS month,
    i.product_type,
    i.item_revenue_cents
  FROM comparison_order_items AS i
)
SELECT
  ir.month,
  ir.product_type,
  AVG(ir.item_revenue_cents) / 100.0 AS avg_item_revenue_usd,
  MAX(ir.item_revenue_cents) / 100.0 AS max_item_revenue_usd
FROM item_rows AS ir
GROUP BY 1, 2
ORDER BY 1, 2
