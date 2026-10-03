-- Portable seed: the suite runs this same script on DuckDB and on Postgres. DuckDB's seed
-- loader splits it on semicolons, so comments here have none.
-- Edge cases: no orders in February 2024 (UTC), store b has none from December 2023 to
-- February 2024, order 7 has a NULL amount and order 8 a NULL store, and orders 3, 5 and
-- 10 fall in the previous day, month or quarter in New York, and order 11 in the previous
-- fiscal quarter. Order 11's customer (108) never signed up, and signup 105 has no channel.
CREATE TABLE orders (
  order_id INTEGER, customer_id INTEGER, store_id VARCHAR(8),
  ordered_at TIMESTAMP, order_date DATE, amount DECIMAL(10, 2)
);
INSERT INTO orders VALUES
  (1, 101, 'a', TIMESTAMP '2023-11-03 10:00:00', DATE '2023-11-03', 10.00),
  (2, 102, 'b', TIMESTAMP '2023-11-20 23:30:00', DATE '2023-11-20', 5.00),
  (3, 101, 'a', TIMESTAMP '2023-12-01 02:00:00', DATE '2023-12-01', 7.00),
  (4, 103, 'a', TIMESTAMP '2024-01-15 12:00:00', DATE '2024-01-15', 20.00),
  (5, 104, 'b', TIMESTAMP '2024-03-01 02:00:00', DATE '2024-03-01', 8.00),
  (6, 102, 'a', TIMESTAMP '2024-03-31 23:00:00', DATE '2024-03-31', 4.00),
  (7, 105, 'a', TIMESTAMP '2024-05-06 09:00:00', DATE '2024-05-06', NULL),
  (8, 101, NULL, TIMESTAMP '2024-05-20 09:00:00', DATE '2024-05-20', 6.00),
  (9, 103, 'b', TIMESTAMP '2024-06-30 12:00:00', DATE '2024-06-30', 3.00),
  (10, 106, 'b', TIMESTAMP '2024-07-01 03:30:00', DATE '2024-07-01', 9.00),
  (11, 108, 'a', TIMESTAMP '2024-08-01 02:00:00', DATE '2024-08-01', 2.00);
-- The same instants in a zone-aware column.
ALTER TABLE orders ADD COLUMN ordered_at_tz TIMESTAMP WITH TIME ZONE;
UPDATE orders SET ordered_at_tz = ordered_at AT TIME ZONE 'UTC';

-- DATE and timestamp clocks straddle different window edges; converted dates
-- read as June 30 and July 1 in New York.
CREATE TABLE clock_edges (id INTEGER, source_day DATE, source_time TIMESTAMP, amount INTEGER);
INSERT INTO clock_edges VALUES
  (1, DATE '2024-07-01', TIMESTAMP '2024-07-01 13:00:00', 10),
  (2, DATE '2024-07-02', TIMESTAMP '2024-07-02 01:00:00', 20);

-- Three closed orders take 2, 5 and 1 calendar days. Every other endpoint is NULL.
-- Order 3 closes less than 24 hours later but crosses a day boundary.
ALTER TABLE orders ADD COLUMN closed_at TIMESTAMP;
UPDATE orders SET closed_at = CASE order_id
  WHEN 1 THEN TIMESTAMP '2023-11-05 09:00:00'
  WHEN 2 THEN TIMESTAMP '2023-11-25 01:00:00'
  WHEN 3 THEN TIMESTAMP '2023-12-02 01:00:00'
  ELSE NULL END;

-- Conversion to a first order within 7 days: 101 converts (4 days), 102 does not (exactly
-- 7 days, as the window is half-open), 103 converts (one second inside), 104 does not (its
-- order came first), 105 converts (the same instant), 106 converts in the next month and
-- quarter, and 107 never orders.
CREATE TABLE signups (customer_id INTEGER, signed_up_at TIMESTAMP, channel VARCHAR(8));
INSERT INTO signups VALUES
  (101, TIMESTAMP '2023-10-30 10:00:00', 'web'),
  (102, TIMESTAMP '2023-11-13 23:30:00', 'store'),
  (103, TIMESTAMP '2024-01-08 12:00:01', 'web'),
  (104, TIMESTAMP '2024-03-02 00:00:00', 'store'),
  (105, TIMESTAMP '2024-05-06 09:00:00', NULL),
  (106, TIMESTAMP '2024-06-25 03:30:00', 'web'),
  (107, TIMESTAMP '2024-06-10 08:00:00', 'store');

-- Customer 101 has no version at its first or last order, and order 3 is exactly
-- the boundary between its versions. Customer 104's order is at its validity end.
CREATE TABLE customer_history (customer_id INTEGER, valid_from TIMESTAMP, valid_to TIMESTAMP);
INSERT INTO customer_history VALUES
  (101, TIMESTAMP '2023-11-10 00:00:00', TIMESTAMP '2023-12-01 02:00:00'),
  (101, TIMESTAMP '2023-12-01 02:00:00', TIMESTAMP '2024-01-01 00:00:00'),
  (102, TIMESTAMP '2023-11-20 23:30:00', NULL),
  (104, TIMESTAMP '2024-03-01 00:00:00', TIMESTAMP '2024-03-01 02:00:00');

-- Refunds pivoted by type: a goods refund fills goods_amount and a shipping refund fills
-- shipping_amount, each leaving the other columns NULL, and tax_amount is never filled. Order 8
-- (no store) and every order but 2, 4, 6 and 7 have no refund. Order 2 has two goods refunds,
-- and order 7 (no amount, and a customer with no channel) one of each type.
CREATE TABLE refunds (
  refund_id INTEGER, order_id INTEGER, refund_type VARCHAR(16),
  goods_amount DECIMAL(10, 2), shipping_amount DECIMAL(10, 2), tax_amount DECIMAL(10, 2)
);
INSERT INTO refunds VALUES
  (1, 2, 'goods', 5.00, NULL, NULL),
  (2, 4, 'shipping', NULL, 3.00, NULL),
  (3, 6, 'goods', 4.00, NULL, NULL),
  (4, 2, 'goods', 1.00, NULL, NULL),
  (5, 7, 'goods', 2.00, NULL, NULL),
  (6, 7, 'shipping', NULL, 1.00, NULL);

-- A monthly rollup of orders by store, keyed by a DATE (the base table's key is a TIMESTAMP).
CREATE TABLE orders_monthly AS
SELECT CAST(date_trunc('month', ordered_at) AS DATE) AS month_start, store_id,
  SUM(amount) AS revenue, COUNT(order_id) AS order_count
FROM orders GROUP BY 1, 2;

-- The Gregorian calendar, and a fiscal one whose year starts in February.
CREATE TABLE dim_date AS
SELECT CAST(d AS DATE) AS date_day,
  CAST(date_trunc('week', d) AS DATE) AS week_start,
  CAST(date_trunc('month', d) AS DATE) AS month_start,
  CAST(date_trunc('quarter', d) AS DATE) AS quarter_start,
  CAST(date_trunc('year', d) AS DATE) AS year_start
FROM generate_series(TIMESTAMP '2023-01-01', TIMESTAMP '2024-12-31', INTERVAL '1 day') AS g(d);
CREATE TABLE dim_fiscal AS
SELECT CAST(d AS DATE) AS date_day,
  CAST(date_trunc('week', d) AS DATE) AS week_start,
  CAST(date_trunc('month', d) AS DATE) AS month_start,
  CAST(date_trunc('quarter', d - INTERVAL '1 month') + INTERVAL '1 month' AS DATE) AS quarter_start,
  CAST(date_trunc('year', d - INTERVAL '1 month') + INTERVAL '1 month' AS DATE) AS year_start
FROM generate_series(TIMESTAMP '2023-01-01', TIMESTAMP '2024-12-31', INTERVAL '1 day') AS g(d);
