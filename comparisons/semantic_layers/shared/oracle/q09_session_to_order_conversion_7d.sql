-- q09_session_to_order_conversion_7d
-- Question: What share of sessions converted to an order within 7 days?
-- Expected semantics: Match sessions to later orders for the same customer within a
--   7 day window.
--
-- Interpretation:
--   A session converted if at least one order exists with the same customer_id and
--   session.started_at < order.ordered_at <= session.started_at + 7 days. The rate is
--   converted sessions / all sessions, grouped by the calendar month of
--   session.started_at (the session clock, since sessions are the denominator).
--   Conversion is derived from comparison_orders. The session table's
--   converted_order_id and converted_at are not used.
--
-- Ambiguities resolved:
--   * "Later": the order must be strictly after session start. Alternative: allow
--     ordered_at = started_at. No order has the same timestamp as a session start.
--     Each session's first later order comes 29-54 minutes after it, so this does
--     not matter.
--   * "Within 7 days": at most 7 x 24 hours after started_at, inclusive.
--     Alternative: exclusive, or calendar days. Every session's first later order
--     is within 54 minutes, so this does not matter.
--   * Store is not matched. That is q15.
--   * Several matching orders still count the session once (EXISTS). The rate is
--     session-weighted. An order may convert more than one session. Each session
--     here has a different customer, so that does not arise.
--   * Denominator: every session in the month, including sessions with a NULL
--     customer_id. Those can never match, so they count as not converted. There
--     are none. Right-censoring: every session's 7-day window ends well inside
--     the order data (sessions 2016-09-01, orders to 2017-08-31).
--   * Month groups: only months that have sessions (2016-09).
--   * Cross-check against the precomputed attribution: converted_order_id is set on
--     8 of 10 sessions (rate 0.8). By the stated rule all 10 convert (rate 1.0).
--     SES-007 (customer d1747387...) ordered 45 minutes after its session and
--     SES-010 (customer 90831ddd...) 35 minutes after, at the same store. Neither
--     session has a converted_order_id. The answer follows the rule, not the
--     attribution column.
WITH session_conversion AS (
  SELECT
    s.session_id,
    s.started_at,
    EXISTS (
      SELECT 1
      FROM comparison_orders AS o
      WHERE o.customer_id = s.customer_id
        AND o.ordered_at > s.started_at
        AND o.ordered_at <= s.started_at + INTERVAL 7 DAY
    ) AS converted_7d
  FROM comparison_storefront_sessions AS s
)
SELECT
  CAST(DATE_TRUNC('month', sc.started_at) AS DATE) AS month,
  CAST(COUNT(DISTINCT sc.session_id) FILTER (WHERE sc.converted_7d) AS DOUBLE)
    / COUNT(DISTINCT sc.session_id) AS session_to_order_conversion_rate_7d
FROM session_conversion AS sc
GROUP BY 1
ORDER BY 1
