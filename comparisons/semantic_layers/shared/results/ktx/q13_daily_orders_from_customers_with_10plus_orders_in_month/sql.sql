WITH daily_orders_from_high_frequency_customers_month__customer_months AS (
SELECT customer_id, DATE_TRUNC('MONTH', ordered_at) AS ordered_month, COUNT(*) AS monthly_orders FROM comparison_orders GROUP BY 1, 2
),
daily_orders_from_high_frequency_customers_month AS (
SELECT o.order_id AS qualifying_order_id, DATE_TRUNC('DAY', o.ordered_at) AS ordered_day FROM comparison_orders AS o INNER JOIN daily_orders_from_high_frequency_customers_month__customer_months AS c ON o.customer_id = c.customer_id AND DATE_TRUNC('MONTH', o.ordered_at) = c.ordered_month WHERE c.monthly_orders > 10
)
SELECT DATE_TRUNC('DAY', daily_orders_from_high_frequency_customers_month.ordered_day) AS ordered_day_day, COUNT(DISTINCT daily_orders_from_high_frequency_customers_month.qualifying_order_id) AS qualifying_orders FROM daily_orders_from_high_frequency_customers_month GROUP BY DATE_TRUNC('DAY', daily_orders_from_high_frequency_customers_month.ordered_day) ORDER BY 1 LIMIT 5000