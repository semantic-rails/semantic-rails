SELECT month, SUM(customer_month_orders) AS qualifying_orders
FROM (
  SELECT
    customer_id,
    DATE_TRUNC('month', ordered_at) AS month,
    MEASURE(orders) AS customer_month_orders
  FROM orders
  GROUP BY 1, 2
) AS customer_months
WHERE customer_month_orders > 5
GROUP BY 1
ORDER BY 1
