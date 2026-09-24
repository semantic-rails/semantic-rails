-- q08_revenue_by_customer_segment_as_of_order_time
-- Question: What was revenue by customer segment at the time the order happened?
-- Expected semantics: Use a temporal-validity relationship from orders to customer
--   history and preserve null when no valid history row exists.
--
-- Interpretation:
--   Each order takes the customer_segment of the comparison_customer_history row
--   for the same customer whose validity interval contains the order's ordered_at.
--   Revenue is SUM(order_total_cents) / 100.0, grouped by order month and that
--   segment.
--
-- Ambiguities resolved:
--   * Validity interval is half-open: valid_from <= ordered_at < valid_to, and
--     valid_to NULL means open-ended (the current row). Alternative: a closed
--     interval at valid_to. No order falls exactly on a boundary (all boundaries
--     are at midnight and orders run 07:00-19:59), so this does not matter here.
--   * "Preserve null": a LEFT JOIN keeps every order. If no history row is valid
--     at ordered_at, the order goes to a customer_segment = NULL group, which is
--     emitted as a row. That covers customers with no history at all (935 of 939
--     customers) and would cover gaps in a customer's history (none here).
--     Alternative, not taken: inner join and drop those orders, or fall back to
--     the current segment.
--   * Fan-out: overlapping validity rows for one customer would double-count
--     revenue. Checked: the 7 history rows (4 customers) are contiguous and do not
--     overlap, so each order matches at most one row.
--   * Revenue means order_total_cents including tax, the same as q02.
--   * Segment history is used exactly as recorded. For example, customer 7cd5e7f3
--     stays 'new' for the whole period despite 159 orders. It is not re-derived
--     from order counts.
--   * Month-segment pairs with no orders are omitted. 'high_value' (valid from
--     2018-01-01) never overlaps the order data (2016-09 to 2017-08), so it has no
--     rows. 'repeat' appears from 2017-01 on.
SELECT
  CAST(DATE_TRUNC('month', o.ordered_at) AS DATE) AS month,
  h.customer_segment,
  SUM(o.order_total_cents) / 100.0 AS revenue_usd
FROM comparison_orders AS o
LEFT JOIN comparison_customer_history AS h
  ON h.customer_id = o.customer_id
 AND o.ordered_at >= h.valid_from
 AND (h.valid_to IS NULL OR o.ordered_at < h.valid_to)
GROUP BY 1, 2
ORDER BY 1, 2 NULLS LAST
