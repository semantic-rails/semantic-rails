WITH session_conversion_7d__matched AS (
SELECT s.session_id, DATE_TRUNC('MONTH', s.started_at) AS session_month, MIN(o.ordered_at) AS first_ordered_at FROM comparison_storefront_sessions AS s LEFT JOIN comparison_orders AS o ON s.customer_id = o.customer_id AND o.ordered_at >= s.started_at AND o.ordered_at < s.started_at + INTERVAL '7' DAY GROUP BY 1, 2
),
session_conversion_7d AS (
SELECT session_id, session_month, CASE WHEN NOT first_ordered_at IS NULL THEN 1.0 ELSE 0.0 END AS converted_within_7d FROM session_conversion_7d__matched AS matched
)
SELECT DATE_TRUNC('MONTH', session_conversion_7d.session_month) AS session_month_month, AVG(session_conversion_7d.converted_within_7d) AS session_to_order_conversion_rate_7d FROM session_conversion_7d GROUP BY DATE_TRUNC('MONTH', session_conversion_7d.session_month) ORDER BY 1 LIMIT 5000