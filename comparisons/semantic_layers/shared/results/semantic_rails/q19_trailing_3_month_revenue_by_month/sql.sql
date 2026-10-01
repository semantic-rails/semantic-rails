WITH leaf_1 AS (
SELECT
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) AS t,
  SUM(comparison_orders.order_total_cents / 100.0) AS m1
FROM comparison_orders
GROUP BY
  DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))
),
leaf_base AS (
SELECT
  base.t AS t,
  base.m1 AS m1
FROM leaf_1 AS base
),
dense_bounds AS (
SELECT
  MIN(leaf_base.t) AS range_start,
  MAX(leaf_base.t) AS range_end
FROM leaf_base
),
implicit_days AS (
SELECT
  CAST(day_series.series_day AS DATE) AS date_day
FROM dense_bounds
CROSS JOIN LATERAL GENERATE_SERIES(CAST(dense_bounds.range_start AS DATE), CAST(dense_bounds.range_end AS DATE), INTERVAL (1) DAY) AS day_series(series_day)
),
implicit_calendar AS (
SELECT
  implicit_days.date_day AS date_day,
  DATE_TRUNC('month', CAST(implicit_days.date_day AS TIMESTAMP)) AS bucket
FROM implicit_days
),
calendar_time AS (
SELECT
  implicit_calendar.bucket AS t
FROM implicit_calendar
CROSS JOIN dense_bounds
WHERE
  implicit_calendar.bucket >= dense_bounds.range_start
  AND implicit_calendar.bucket <= dense_bounds.range_end
GROUP BY
  implicit_calendar.bucket
),
leaf_time_keys AS (
SELECT
  leaf_base.t AS t,
  1 AS source_present
FROM leaf_base
WHERE
  leaf_base.t IS NOT NULL
GROUP BY
  leaf_base.t
),
dense_time AS (
SELECT
  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END AS t
FROM calendar_time
FULL OUTER JOIN leaf_time_keys ON calendar_time.t = leaf_time_keys.t
GROUP BY
  CASE WHEN leaf_time_keys.source_present = 1 THEN leaf_time_keys.t ELSE calendar_time.t END
),
series_base AS (
SELECT
  dense_time.t AS t,
  leaf_base.m1 AS m1
FROM dense_time
LEFT JOIN leaf_base ON dense_time.t = leaf_base.t
),
coverage_1 AS (
SELECT
  MIN(DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP))) AS loaded_from,
  MAX(CASE WHEN CASE WHEN CAST(PG_TYPEOF(comparison_orders.ordered_at) AS VARCHAR) = 'timestamp with time zone' THEN TIMEZONE('UTC', CAST(comparison_orders.ordered_at AS TIMESTAMPTZ)) ELSE TIMEZONE('UTC', TIMEZONE('UTC', CAST(comparison_orders.ordered_at AS TIMESTAMP))) END <= TIMEZONE('UTC', NOW()) THEN DATE_TRUNC('month', CAST(comparison_orders.ordered_at AS TIMESTAMP)) END) AS loaded_to
FROM comparison_orders
),
guarded_base AS (
SELECT
  base.t AS t,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND (base.t >= coverage_1.loaded_from AND base.t <= coverage_1.loaded_to) THEN 0 END) AS m1
FROM series_base AS base
CROSS JOIN coverage_1
)
SELECT
  base.t AS "temporal_role.jaffle_order_time__month",
  SUM(base.m1) OVER (ORDER BY base.t ASC ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS trailing_3_month_revenue_usd
FROM guarded_base AS base
ORDER BY
  t ASC