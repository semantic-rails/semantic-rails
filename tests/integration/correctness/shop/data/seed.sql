-- Portable seed: the suite runs this same script on DuckDB and on Postgres, which splits
-- it on semicolons, so comments here have none.
-- Edge cases: no orders in February 2024 (UTC), store b has none from December 2023 to
-- February 2024, order 7 has a NULL amount and order 8 a NULL store, and orders 3, 5 and
-- 10 fall in the previous day, month or quarter in New York.
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
  (10, 106, 'b', TIMESTAMP '2024-07-01 03:30:00', DATE '2024-07-01', 9.00);
-- The same instants in a zone-aware column.
ALTER TABLE orders ADD COLUMN ordered_at_tz TIMESTAMP WITH TIME ZONE;
UPDATE orders SET ordered_at_tz = ordered_at AT TIME ZONE 'UTC';

-- Conversion to a first order within 7 days: 101 converts (4 days), 102 does not (exactly
-- 7 days, as the window is half-open), 103 converts (one second inside), 104 does not (its
-- order came first), 105 converts (the same instant), 106 converts in the next month and
-- quarter, and 107 never orders.
CREATE TABLE signups (customer_id INTEGER, signed_up_at TIMESTAMP);
INSERT INTO signups VALUES
  (101, TIMESTAMP '2023-10-30 10:00:00'),
  (102, TIMESTAMP '2023-11-13 23:30:00'),
  (103, TIMESTAMP '2024-01-08 12:00:01'),
  (104, TIMESTAMP '2024-03-02 00:00:00'),
  (105, TIMESTAMP '2024-05-06 09:00:00'),
  (106, TIMESTAMP '2024-06-25 03:30:00'),
  (107, TIMESTAMP '2024-06-10 08:00:00');

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
