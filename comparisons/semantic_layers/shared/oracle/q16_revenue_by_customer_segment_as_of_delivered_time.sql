-- q16_revenue_by_customer_segment_as_of_delivered_time
-- Question: What was delivered revenue by customer segment as of delivered time?
-- Expected semantics: Use delivered time as the business clock for the measure and for
--   the temporal-valid join into customer history.
--
-- Interpretation:
--   For each order in comparison_order_lifecycle:
--     month   = calendar month of delivered_at (not ordered_at),
--     segment = customer_segment of the history row for the order's customer that is
--               valid at delivered_at: valid_from <= delivered_at < valid_to, with
--               valid_to NULL meaning open-ended,
--     delivered_revenue = SUM(order_total_cents) / 100.0 in USD.
--   A LEFT JOIN keeps orders that have no history row valid at delivered_at, in
--   the customer_segment = NULL group.
--
-- Ambiguities resolved:
--   * Both the month bucket and the validity lookup use delivered_at. Mixing clocks
--     (month by delivered_at, segment as of ordered_at, or the reverse) is the
--     alternative. It gives the same answer here.
--   * Half-open validity interval, preserved NULLs and no fan-out: the same as q08.
--     The history rows do not overlap.
--   * Revenue means order_total_cents including tax, the same as q02 and q07.
--     Orders with a NULL delivered_at would be excluded. There are none.
--   * Data note: delivered_at is always within 27-49 minutes of ordered_at on the
--     same day. History boundaries are at midnight on Jan 1 and orders run
--     07:00-19:59. So no order changes month or segment between the two clocks,
--     and on this data the result equals q08 row for row.
--   * Month-segment pairs with no deliveries are omitted, so 'high_value' has no
--     rows.
SELECT
  CAST(DATE_TRUNC('month', l.delivered_at) AS DATE) AS month,
  h.customer_segment,
  SUM(l.order_total_cents) / 100.0 AS delivered_revenue
FROM comparison_order_lifecycle AS l
LEFT JOIN comparison_customer_history AS h
  ON h.customer_id = l.customer_id
 AND l.delivered_at >= h.valid_from
 AND (h.valid_to IS NULL OR l.delivered_at < h.valid_to)
WHERE l.delivered_at IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2 NULLS LAST
