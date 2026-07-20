WITH delivered_revenue_by_customer_segment AS (
select
  l.order_id,
  date_trunc('month', l.delivered_at) as delivered_month,
  h.customer_segment,
  l.order_total_cents / 100.0 as delivered_revenue_usd
from comparison_order_lifecycle as l
left join comparison_customer_history as h
  on l.customer_id = h.customer_id
 and l.delivered_at >= h.valid_from
 and (h.valid_to is null or l.delivered_at < h.valid_to)
)
SELECT DATE_TRUNC('MONTH', delivered_revenue_by_customer_segment.delivered_month) AS delivered_month_month, delivered_revenue_by_customer_segment.customer_segment AS customer_segment, SUM(delivered_revenue_by_customer_segment.delivered_revenue_usd) AS delivered_revenue FROM delivered_revenue_by_customer_segment GROUP BY DATE_TRUNC('MONTH', delivered_revenue_by_customer_segment.delivered_month), delivered_revenue_by_customer_segment.customer_segment ORDER BY 1, 2 LIMIT 5000