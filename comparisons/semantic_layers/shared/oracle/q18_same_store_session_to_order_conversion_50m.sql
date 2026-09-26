-- q18_same_store_session_to_order_conversion_50m
-- Question: What share of sessions converted to an order at the same store within 50 minutes?
-- Expected semantics: The q15 rule with a 50 minute window. A session converted if the same
--   customer placed an order at the session's store in [session start, session start +
--   50 minutes).
--
-- Interpretation:
--   A session converted if at least one order exists with the same customer_id and the
--   same store_id, and session.started_at <= order.ordered_at < session.started_at +
--   50 minutes. Rate = converted sessions / all sessions, by the calendar month of
--   session.started_at, as in q15. The session table's converted_order_id and
--   converted_at are not used.
--
-- Ambiguities resolved:
--   * Window bounds: half-open [start, start + 50 minutes), the usual duration
--     semantics and the rule the question states. The older q09 and q15 keys use
--     (start, start + 7 days]. On this data no order falls on either boundary, so both
--     give the same answer. No session's customer has an order at or before the
--     session start. Nearest the end boundary: SES-002's order at 48 minutes (2 minutes
--     inside) and SES-009's at 52 minutes (2 minutes outside). All timestamps are whole
--     minutes.
--   * Same-store matching compares store_id values. A NULL store never matches. There
--     are none. All 10 sessions and all their candidate orders are at Philadelphia, so
--     dropping the store condition gives the same answer (0.8).
--   * Several matching orders still count the session once (EXISTS). Counting matching
--     orders instead gives 8 orders / 10 sessions = 0.8 too: each converted session
--     has exactly one order in its 50 minutes.
--   * Denominator: every session in the month. NULL-customer sessions would count as
--     not converted. There are none.
--   * Month groups: only months that have sessions (2016-09).
--   * Data note: the first later orders come 29-54 minutes after their sessions. SES-005
--     (54 minutes) and SES-009 (52 minutes) fall outside the window, so 8 of 10 convert.
--   * Cross-check against the precomputed attribution: converted_order_id also gives
--     0.8, but by coincidence. It leaves out SES-007 and SES-010, which this rule
--     converts, and keeps SES-005 and SES-009, which this rule does not. A layer that
--     read the attribution column instead of applying the window would match this key.
--   * Discrimination: this answer (0.8) differs from q15's 1.0, so a layer that returned
--     the 7-day metric unchanged would not match.
WITH session_conversion AS (
  SELECT
    s.session_id,
    s.started_at,
    EXISTS (
      SELECT 1
      FROM comparison_orders AS o
      WHERE o.customer_id = s.customer_id
        AND o.store_id = s.store_id
        AND o.ordered_at >= s.started_at
        AND o.ordered_at < s.started_at + INTERVAL 50 MINUTE
    ) AS converted_same_store_50m
  FROM comparison_storefront_sessions AS s
)
SELECT
  CAST(DATE_TRUNC('month', sc.started_at) AS DATE) AS month,
  CAST(COUNT(DISTINCT sc.session_id) FILTER (WHERE sc.converted_same_store_50m) AS DOUBLE)
    / COUNT(DISTINCT sc.session_id) AS same_store_conversion_rate_50m
FROM session_conversion AS sc
GROUP BY 1
ORDER BY 1
