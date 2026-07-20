WITH revenue_by_customer_segment_order_time AS (
select
  o.order_id,
  date_trunc('month', o.ordered_at) as ordered_month,
  h.customer_segment,
  o.order_total_cents / 100.0 as revenue_usd
from comparison_orders as o
left join comparison_customer_history as h
  on o.customer_id = h.customer_id
 and o.ordered_at >= h.valid_from
 and (h.valid_to is null or o.ordered_at < h.valid_to)
)
SELECT DATE_TRUNC('MONTH', revenue_by_customer_segment_order_time.ordered_month) AS ordered_month_month, revenue_by_customer_segment_order_time.customer_segment AS customer_segment, SUM(revenue_by_customer_segment_order_time.revenue_usd) AS revenue_usd FROM revenue_by_customer_segment_order_time GROUP BY DATE_TRUNC('MONTH', revenue_by_customer_segment_order_time.ordered_month), revenue_by_customer_segment_order_time.customer_segment ORDER BY 1, 2 LIMIT 5000