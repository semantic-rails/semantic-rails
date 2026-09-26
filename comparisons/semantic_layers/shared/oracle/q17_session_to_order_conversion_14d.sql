-- q17_session_to_order_conversion_14d
-- Question: What share of sessions converted to an order within 14 days?
-- Expected semantics: The q09 rule with a 14 day window. A session converted if the same
--   customer placed an order in [session start, session start + 14 x 24 hours).
--
-- Interpretation:
--   A session converted if at least one order exists with the same customer_id and
--   session.started_at <= order.ordered_at < session.started_at + 14 x 24 hours.
--   Timestamps are naive, so INTERVAL 14 DAY is exactly 14 x 24 hours. The rate is
--   converted sessions / all sessions, grouped by the calendar month of
--   session.started_at, as in q09. Conversion is derived from comparison_orders. The
--   session table's converted_order_id and converted_at are not used.
--
-- Ambiguities resolved:
--   * Window bounds: half-open [start, start + 14 days), the usual duration semantics
--     and the rule the question states. The older q09 and q15 keys use
--     (start, start + 7 days]. On this data no order falls on either boundary, so
--     both give the same answer. No session's customer has an order at or before the
--     session start (the earliest later order is 29 minutes after, SES-006). The
--     order nearest the end boundary is SES-007's at 2016-09-15 07:28, 13 minutes
--     outside it. The nearest inside is SES-006's at 2016-09-14 13:05, 19 hours
--     53 minutes before it.
--   * 14 x 24 hours, not 14 calendar days ending at midnight. Every session's first
--     later order is within 54 minutes, so this does not matter.
--   * Store is not matched (that is q15). Several matching orders still count the
--     session once (EXISTS). Alternative: count matching orders instead of converted
--     sessions. That changes the answer: 70 orders / 10 sessions = 7.0 instead of 1.0.
--   * Denominator: every session in the month. A NULL-customer session could never
--     match and would count as not converted. There are none. Right-censoring: every
--     14-day window ends 2016-09-15, well inside the order data (to 2017-08-31).
--   * Month groups: only months that have sessions (2016-09).
--   * Cross-check against the precomputed attribution: converted_order_id is set on 8
--     of 10 sessions (0.8). By the stated rule all 10 convert (1.0), as in q09.
--   * Discrimination: this answer equals q09's 1.0 (and q15's). Every session's first
--     later order comes 29-54 minutes after it, so any window of an hour or more gives
--     1.0. A layer that returned the 7-day metric unchanged would still match this key.
WITH session_conversion AS (
  SELECT
    s.session_id,
    s.started_at,
    EXISTS (
      SELECT 1
      FROM comparison_orders AS o
      WHERE o.customer_id = s.customer_id
        AND o.ordered_at >= s.started_at
        AND o.ordered_at < s.started_at + INTERVAL 14 DAY
    ) AS converted_14d
  FROM comparison_storefront_sessions AS s
)
SELECT
  CAST(DATE_TRUNC('month', sc.started_at) AS DATE) AS month,
  CAST(COUNT(DISTINCT sc.session_id) FILTER (WHERE sc.converted_14d) AS DOUBLE)
    / COUNT(DISTINCT sc.session_id) AS session_to_order_conversion_rate_14d
FROM session_conversion AS sc
GROUP BY 1
ORDER BY 1
