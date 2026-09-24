-- q15_same_store_session_to_order_conversion_7d
-- Question: What share of sessions converted to an order at the same store within 7 days?
-- Expected semantics: Match sessions to later orders for the same customer within a 7 day
--   window and require the store property to match across the event pair.
--
-- Interpretation:
--   The same rule as q09, plus order.store_id = session.store_id. A session converted
--   if at least one order exists with the same customer_id and the same store_id,
--   and session.started_at < order.ordered_at <= session.started_at + 7 days.
--   Rate = converted sessions / all sessions, by the calendar month of
--   session.started_at. The session table's converted_order_id and converted_at are
--   not used.
--
-- Ambiguities resolved:
--   * Same-store matching compares store_id values. A session or order with a NULL
--     store never matches. There are none.
--   * The denominator is all sessions in the month, not only sessions at stores that
--     had orders, and not only sessions that converted anywhere. A session that
--     converted only at another store counts as not converted.
--   * "Later" is strict, the window is inclusive at 7 x 24 hours, NULL-customer
--     sessions count as not converted, and the rate is session-weighted. These are
--     the same choices as q09, and on this data none of them changes the answer.
--   * Data note: all 10 sessions (2016-09-01) are at Philadelphia, and Brooklyn
--     only opened on 2017-03-12, so every candidate order is at Philadelphia. The
--     same-store condition removes nothing and the result equals q09: 1.0 derived,
--     against 0.8 if the precomputed converted_order_id were used.
WITH session_conversion AS (
  SELECT
    s.session_id,
    s.started_at,
    EXISTS (
      SELECT 1
      FROM comparison_orders AS o
      WHERE o.customer_id = s.customer_id
        AND o.store_id = s.store_id
        AND o.ordered_at > s.started_at
        AND o.ordered_at <= s.started_at + INTERVAL 7 DAY
    ) AS converted_same_store_7d
  FROM comparison_storefront_sessions AS s
)
SELECT
  CAST(DATE_TRUNC('month', sc.started_at) AS DATE) AS month,
  CAST(COUNT(DISTINCT sc.session_id) FILTER (WHERE sc.converted_same_store_7d) AS DOUBLE)
    / COUNT(DISTINCT sc.session_id) AS same_store_conversion_rate_7d
FROM session_conversion AS sc
GROUP BY 1
ORDER BY 1
