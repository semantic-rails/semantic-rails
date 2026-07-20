CREATE OR REPLACE TABLE jaffle_store AS
SELECT
  CAST(id AS VARCHAR) AS store_id,
  CAST(name AS VARCHAR) AS store_name,
  CAST(opened_at AS TIMESTAMP) AS opened_at,
  CAST(tax_rate AS DOUBLE) AS tax_rate
FROM raw_stores;

CREATE OR REPLACE TABLE jaffle_supply AS
SELECT
  CAST(id AS VARCHAR) AS supply_id,
  CAST(name AS VARCHAR) AS supply_name,
  CAST(cost AS BIGINT) AS supply_cost_cents,
  LOWER(CAST(perishable AS VARCHAR)) = 'true' AS is_perishable,
  CAST(sku AS VARCHAR) AS sku
FROM raw_supplies;

CREATE OR REPLACE TABLE jaffle_supply_cost AS
SELECT
  sku,
  SUM(supply_cost_cents) AS total_supply_cost_cents,
  COUNT(*) AS supply_component_count,
  SUM(CASE WHEN is_perishable THEN 1 ELSE 0 END) AS perishable_component_count
FROM jaffle_supply
GROUP BY 1;

CREATE OR REPLACE TABLE jaffle_product AS
SELECT
  CAST(p.sku AS VARCHAR) AS sku,
  CAST(p.name AS VARCHAR) AS product_name,
  CAST(p.type AS VARCHAR) AS product_type,
  CAST(p.price AS BIGINT) AS price_cents,
  CAST(p.description AS VARCHAR) AS description,
  COALESCE(sc.total_supply_cost_cents, 0) AS total_supply_cost_cents,
  COALESCE(sc.supply_component_count, 0) AS supply_component_count,
  COALESCE(sc.perishable_component_count, 0) AS perishable_component_count
FROM raw_products AS p
LEFT JOIN jaffle_supply_cost AS sc
  ON CAST(p.sku AS VARCHAR) = sc.sku;

CREATE OR REPLACE TABLE jaffle_item AS
SELECT
  CAST(i.id AS VARCHAR) AS item_id,
  CAST(i.order_id AS VARCHAR) AS order_id,
  CAST(i.sku AS VARCHAR) AS sku,
  p.product_name,
  p.product_type,
  p.price_cents AS item_revenue_cents,
  COALESCE(sc.total_supply_cost_cents, 0) AS item_cost_cents,
  p.price_cents - COALESCE(sc.total_supply_cost_cents, 0) AS item_margin_cents
FROM raw_items AS i
LEFT JOIN jaffle_product AS p
  ON CAST(i.sku AS VARCHAR) = p.sku
LEFT JOIN jaffle_supply_cost AS sc
  ON CAST(i.sku AS VARCHAR) = sc.sku;

CREATE OR REPLACE TABLE jaffle_order AS
WITH ranked_orders AS (
  SELECT
    CAST(id AS VARCHAR) AS order_id,
    CAST(customer AS VARCHAR) AS customer_id,
    CAST(ordered_at AS TIMESTAMP) AS ordered_at,
    CAST(store_id AS VARCHAR) AS store_id,
    CAST(subtotal AS BIGINT) AS subtotal_cents,
    CAST(tax_paid AS BIGINT) AS tax_paid_cents,
    CAST(order_total AS BIGINT) AS order_total_cents,
    ROW_NUMBER() OVER (
      PARTITION BY CAST(customer AS VARCHAR)
      ORDER BY CAST(ordered_at AS TIMESTAMP), CAST(id AS VARCHAR)
    ) AS customer_order_number
  FROM raw_orders
),
order_item_rollup AS (
  SELECT
    order_id,
    COUNT(*) AS item_count,
    SUM(CASE WHEN LOWER(product_type) = 'jaffle' THEN 1 ELSE 0 END) AS food_item_count,
    SUM(CASE WHEN LOWER(product_type) = 'beverage' THEN 1 ELSE 0 END) AS drink_item_count,
    SUM(item_revenue_cents) AS item_revenue_cents,
    SUM(item_cost_cents) AS item_cost_cents,
    SUM(item_margin_cents) AS item_margin_cents,
    SUM(CASE WHEN LOWER(product_type) = 'jaffle' THEN item_revenue_cents ELSE 0 END) AS food_revenue_cents,
    SUM(CASE WHEN LOWER(product_type) = 'beverage' THEN item_revenue_cents ELSE 0 END) AS drink_revenue_cents
  FROM jaffle_item
  GROUP BY 1
)
SELECT
  r.order_id,
  r.customer_id,
  r.ordered_at,
  CAST(r.ordered_at AS DATE) AS ordered_date,
  r.store_id,
  r.subtotal_cents,
  r.tax_paid_cents,
  r.order_total_cents,
  r.customer_order_number,
  r.customer_order_number = 1 AS is_new_customer_order,
  r.order_total_cents >= 3000 AS is_large_order,
  COALESCE(o.item_count, 0) AS item_count,
  COALESCE(o.food_item_count, 0) AS food_item_count,
  COALESCE(o.drink_item_count, 0) AS drink_item_count,
  COALESCE(o.food_item_count, 0) > 0 AS has_food_item,
  COALESCE(o.drink_item_count, 0) > 0 AS has_drink_item,
  COALESCE(o.item_revenue_cents, r.subtotal_cents) AS item_revenue_cents,
  COALESCE(o.item_cost_cents, 0) AS order_cost_cents,
  COALESCE(o.food_revenue_cents, 0) AS food_revenue_cents,
  COALESCE(o.drink_revenue_cents, 0) AS drink_revenue_cents,
  r.subtotal_cents - COALESCE(o.item_cost_cents, 0) AS gross_profit_cents
FROM ranked_orders AS r
LEFT JOIN order_item_rollup AS o
  ON r.order_id = o.order_id;

CREATE OR REPLACE TABLE jaffle_customer AS
WITH customer_rollup AS (
  SELECT
    customer_id,
    MIN(ordered_at) AS first_ordered_at,
    MAX(ordered_at) AS latest_ordered_at,
    COUNT(*) AS lifetime_order_count,
    SUM(subtotal_cents) AS lifetime_spend_before_tax_cents,
    SUM(order_total_cents) AS lifetime_spend_cents,
    SUM(tax_paid_cents) AS lifetime_tax_paid_cents
  FROM jaffle_order
  GROUP BY 1
)
SELECT
  CAST(c.id AS VARCHAR) AS customer_id,
  CAST(c.name AS VARCHAR) AS customer_name,
  cr.first_ordered_at AS first_order_at,
  cr.latest_ordered_at,
  COALESCE(cr.lifetime_order_count, 0) AS lifetime_order_count,
  COALESCE(cr.lifetime_spend_before_tax_cents, 0) AS lifetime_spend_before_tax_cents,
  COALESCE(cr.lifetime_spend_cents, 0) AS lifetime_spend_cents,
  COALESCE(cr.lifetime_tax_paid_cents, 0) AS lifetime_tax_paid_cents,
  CASE
    WHEN COALESCE(cr.lifetime_order_count, 0) > 1 THEN 'repeat'
    ELSE 'new'
  END AS customer_type
FROM raw_customers AS c
LEFT JOIN customer_rollup AS cr
  ON CAST(c.id AS VARCHAR) = cr.customer_id;

CREATE OR REPLACE TABLE jaffle_customer_history AS
SELECT
  CAST(customer_id AS VARCHAR) AS customer_id,
  CAST(segment AS VARCHAR) AS customer_segment,
  CAST(preferred_store_id AS VARCHAR) AS preferred_store_id,
  CAST(valid_from AS TIMESTAMP) AS valid_from,
  CAST(NULLIF(valid_to, 'NULL') AS TIMESTAMP) AS valid_to
FROM raw_customer_history;

CREATE OR REPLACE TABLE jaffle_calendar AS
WITH bounds AS (
  SELECT
    CAST(MIN(CAST(ordered_at AS DATE)) - INTERVAL 365 DAY AS DATE) AS min_order_date,
    CAST(MAX(CAST(ordered_at AS DATE)) + INTERVAL 365 DAY AS DATE) AS max_order_date
  FROM raw_orders
)
SELECT
  CAST(gs.calendar_date AS DATE) AS date_day,
  CAST(DATE_TRUNC('week', gs.calendar_date) AS DATE) AS week_start,
  CAST(DATE_TRUNC('month', gs.calendar_date) AS DATE) AS month_start,
  CAST(DATE_TRUNC('quarter', gs.calendar_date) AS DATE) AS quarter_start,
  CAST(DATE_TRUNC('year', gs.calendar_date) AS DATE) AS year_start
FROM bounds,
LATERAL generate_series(bounds.min_order_date, bounds.max_order_date, INTERVAL 1 DAY) AS gs(calendar_date);

CREATE OR REPLACE TABLE jaffle_calendar_fiscal AS
WITH fiscal_base AS (
  SELECT
    CAST(date_day AS DATE) AS date_day,
    CAST(date_day - INTERVAL 1 MONTH AS DATE) AS shifted_date
  FROM raw_calendar_fiscal
)
SELECT
  date_day,
  CAST(DATE_TRUNC('week', date_day) AS DATE) AS week_start,
  CAST(DATE_TRUNC('month', shifted_date) + INTERVAL 1 MONTH AS DATE) AS month_start,
  CAST(DATE_TRUNC('quarter', shifted_date) + INTERVAL 1 MONTH AS DATE) AS quarter_start,
  CAST(DATE_TRUNC('year', shifted_date) + INTERVAL 1 MONTH AS DATE) AS year_start
FROM fiscal_base;

CREATE OR REPLACE TABLE jaffle_store_inventory_snapshot AS
SELECT
  CAST(store_id AS VARCHAR) AS store_id,
  CAST(date_day AS DATE) AS date_day,
  CAST(inventory_on_hand AS BIGINT) AS inventory_on_hand,
  CAST(active_menu_count AS BIGINT) AS active_menu_count,
  CAST(open_store_count AS BIGINT) AS open_store_count
FROM raw_store_inventory_snapshots;

-- Lifecycle covers every order: hand-authored event rows win where present,
-- otherwise prepared/delivered timestamps are derived from ordered_at with a
-- deterministic per-order jitter (minute component) so delivered-grain
-- rollups span the same date range as ordered-grain ones.
CREATE OR REPLACE TABLE jaffle_order_lifecycle AS
SELECT
  o.order_id,
  o.customer_id,
  o.store_id,
  o.subtotal_cents,
  o.order_total_cents,
  o.ordered_at,
  COALESCE(
    CAST(raw.prepared_at AS TIMESTAMP),
    o.ordered_at + INTERVAL 8 MINUTE
      + CAST(EXTRACT(MINUTE FROM o.ordered_at) % 11 AS INTEGER) * INTERVAL 1 MINUTE
  ) AS prepared_at,
  COALESCE(
    CAST(raw.delivered_at AS TIMESTAMP),
    o.ordered_at + INTERVAL 27 MINUTE
      + CAST(EXTRACT(MINUTE FROM o.ordered_at) % 23 AS INTEGER) * INTERVAL 1 MINUTE
  ) AS delivered_at
FROM jaffle_order AS o
LEFT JOIN raw_order_lifecycle_events AS raw
  ON o.order_id = CAST(raw.order_id AS VARCHAR);

CREATE OR REPLACE TABLE jaffle_storefront_session AS
SELECT
  CAST(s.session_id AS VARCHAR) AS session_id,
  CAST(s.customer_id AS VARCHAR) AS customer_id,
  CAST(s.store_id AS VARCHAR) AS store_id,
  CAST(s.started_at AS TIMESTAMP) AS started_at,
  CAST(NULLIF(s.converted_order_id, 'NULL') AS VARCHAR) AS converted_order_id,
  o.ordered_at AS converted_at
FROM raw_storefront_sessions AS s
LEFT JOIN jaffle_order AS o
  ON CAST(NULLIF(s.converted_order_id, 'NULL') AS VARCHAR) = o.order_id;

CREATE OR REPLACE TABLE jaffle_daily_metric_rollup AS
WITH daily_order_revenue AS (
  SELECT
    ordered_date AS date_day,
    SUM(order_total_cents) AS revenue_cents
  FROM jaffle_order
  GROUP BY 1
),
daily_item_stats AS (
  SELECT
    o.ordered_date AS date_day,
    MEDIAN(i.item_revenue_cents) AS median_item_revenue_cents
  FROM jaffle_item AS i
  INNER JOIN jaffle_order AS o
    ON i.order_id = o.order_id
  GROUP BY 1
),
calendar_daily AS (
  SELECT
    c.date_day,
    c.week_start,
    c.month_start,
    c.quarter_start,
    c.year_start,
    COALESCE(r.revenue_cents, 0) AS revenue_cents,
    COALESCE(s.median_item_revenue_cents, 0) AS median_item_revenue_cents
  FROM jaffle_calendar AS c
  LEFT JOIN daily_order_revenue AS r
    ON c.date_day = r.date_day
  LEFT JOIN daily_item_stats AS s
    ON c.date_day = s.date_day
)
SELECT
  date_day,
  week_start,
  month_start,
  quarter_start,
  year_start,
  revenue_cents,
  median_item_revenue_cents,
  SUM(revenue_cents) OVER (
    ORDER BY date_day
    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
  ) AS rolling_7d_revenue_cents,
  SUM(revenue_cents) OVER (
    ORDER BY date_day
    ROWS BETWEEN 27 PRECEDING AND CURRENT ROW
  ) AS rolling_28d_revenue_cents,
  SUM(revenue_cents) OVER (
    PARTITION BY month_start
    ORDER BY date_day
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  ) AS revenue_mtd_cents,
  SUM(revenue_cents) OVER (
    PARTITION BY quarter_start
    ORDER BY date_day
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  ) AS revenue_qtd_cents,
  SUM(revenue_cents) OVER (
    PARTITION BY year_start
    ORDER BY date_day
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  ) AS revenue_ytd_cents
FROM calendar_daily;

CREATE OR REPLACE TABLE jaffle_monthly_metric_rollup AS
WITH monthly_revenue AS (
  SELECT
    month_start,
    SUM(revenue_cents) AS revenue_cents
  FROM jaffle_daily_metric_rollup
  GROUP BY 1
)
SELECT
  month_start,
  revenue_cents,
  LAG(revenue_cents) OVER (ORDER BY month_start) AS prior_period_revenue_cents,
  CASE
    WHEN COALESCE(LAG(revenue_cents) OVER (ORDER BY month_start), 0) = 0 THEN NULL
    ELSE (
      revenue_cents - LAG(revenue_cents) OVER (ORDER BY month_start)
    )::DOUBLE / NULLIF(LAG(revenue_cents) OVER (ORDER BY month_start), 0)
  END AS month_over_month_revenue_growth_ratio
FROM monthly_revenue;

-- customer_segment_membership: a junction-style table that records
-- which customers belong to which named segment. Loaded directly from
-- jaffle_customer_history's segment field. Used as the `bridge: false`
-- demonstration — the corresponding semantic model opts out of
-- inferred multi-hop joins so the planner cannot accidentally
-- traverse customer → segment → other_customer fanouts.
CREATE OR REPLACE TABLE jaffle_customer_segment_membership AS
SELECT
  customer_id || ':' || customer_segment AS membership_id,
  customer_id,
  customer_segment AS segment_name,
  valid_from AS member_since,
  valid_to AS member_until,
  CASE WHEN valid_to IS NULL THEN 'active' ELSE 'historic' END AS membership_status
FROM jaffle_customer_history;
