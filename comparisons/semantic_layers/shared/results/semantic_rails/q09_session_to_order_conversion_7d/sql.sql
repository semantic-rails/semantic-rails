WITH conversion_leaf_1__conversion_base_1 AS (
SELECT
  DATE_TRUNC('month', CAST(jaffle_storefront_session.started_at AS TIMESTAMP)) AS t,
  jaffle_storefront_session.started_at AS __base_event_time,
  jaffle_storefront_session.session_id AS __base_event_key,
  jaffle_customer.customer_id AS __match_key_1
FROM jaffle_storefront_session
INNER JOIN jaffle_customer ON jaffle_storefront_session.customer_id = jaffle_customer.customer_id
),
conversion_leaf_1__conversion_converted_1 AS (
SELECT
  jaffle_order.ordered_at AS __converted_event_time,
  jaffle_order.order_id AS __converted_event_key,
  jaffle_customer.customer_id AS __match_key_1
FROM jaffle_order
INNER JOIN jaffle_customer ON jaffle_order.customer_id = jaffle_customer.customer_id
),
conversion_leaf_1__conversion_matches_1 AS (
SELECT
  base_events.t AS t,
  base_events.__base_event_key AS __base_event_key,
  converted_events.__converted_event_key AS __converted_event_key,
  ROW_NUMBER() OVER (PARTITION BY base_events.__base_event_key ORDER BY converted_events.__converted_event_time ASC, converted_events.__converted_event_key ASC) AS __match_rank
FROM conversion_leaf_1__conversion_base_1 AS base_events
LEFT JOIN conversion_leaf_1__conversion_converted_1 AS converted_events ON converted_events.__converted_event_time >= base_events.__base_event_time AND DATE_DIFF('day', CAST(base_events.__base_event_time AS TIMESTAMP), CAST(converted_events.__converted_event_time AS TIMESTAMP)) <= 7 AND base_events.__match_key_1 IS NOT DISTINCT FROM converted_events.__match_key_1
),
conversion_leaf_1 AS (
SELECT
  matches.t AS t,
  COUNT(DISTINCT CASE WHEN matches.__converted_event_key IS NOT NULL THEN matches.__base_event_key END) / NULLIF(COUNT(DISTINCT matches.__base_event_key), 0) AS m1
FROM conversion_leaf_1__conversion_matches_1 AS matches
WHERE
  matches.__match_rank = 1
GROUP BY
  matches.t
)
SELECT
  base.t AS "temporal_role.jaffle_session_started_at__month",
  base.m1 AS session_to_order_conversion_rate_7d
FROM conversion_leaf_1 AS base
ORDER BY
  t ASC