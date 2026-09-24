-- q04_aov_by_store
-- Question: What is average order value by store?
-- Expected semantics: Revenue divided by orders grouped by store.
--
-- Interpretation:
--   For each store, over all time: aov_usd = SUM(order_total_cents) / 100.0
--   divided by COUNT(DISTINCT order_id). This is a ratio of sums over the store's
--   whole history, not an average of monthly AOVs. It is left unrounded.
--
-- Ambiguities resolved:
--   * Revenue means order_total_cents including tax, the same as q02.
--     Alternative: pre-tax subtotal. That gives Brooklyn 11.6212 instead of 12.0859
--     and Philadelphia 12.0186 instead of 12.7391.
--   * The denominator is every order, including the 1,119 empty $0 orders
--     (393 Brooklyn, 726 Philadelphia). Alternative: exclude empty orders. That gives
--     Brooklyn 12.3114 and Philadelphia 12.9860.
--   * No time filter and no month grain: the question has no time dimension.
--   * Stores with no orders have an undefined AOV (0/0), so they are omitted.
--     This applies to Chicago, San Francisco and New Orleans.
SELECT
  s.store_name,
  SUM(o.order_total_cents) / 100.0 / COUNT(DISTINCT o.order_id) AS aov_usd
FROM comparison_orders AS o
LEFT JOIN comparison_stores AS s
  ON s.store_id = o.store_id
GROUP BY 1
ORDER BY 1
