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
-- Customers 101 and 103 share a name, and each has the most revenue.
ALTER TABLE signups ADD COLUMN name VARCHAR(8);
UPDATE signups SET name = CASE customer_id
  WHEN 101 THEN 'Ann' WHEN 102 THEN 'Bea' WHEN 103 THEN 'Ann' WHEN 104 THEN 'Cy'
  WHEN 105 THEN 'Di' WHEN 106 THEN 'Ed' ELSE 'Flo' END;

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

-- Shipment scans have no key. Order 1's pickup is stored twice, an exact duplicate, and both rows
-- count. One scan has no order and one names order 99, which doesn't exist. Orders 2, 4 and 6
-- have a refund.
CREATE TABLE shipment_scans (
  order_id INTEGER, scanned_at TIMESTAMP, scan_type VARCHAR(16), weight DECIMAL(10, 2)
);
INSERT INTO shipment_scans VALUES
  (1, TIMESTAMP '2023-11-04 08:00:00', 'pickup', 1.50),
  (1, TIMESTAMP '2023-11-04 08:00:00', 'pickup', 1.50),
  (1, TIMESTAMP '2023-11-06 17:00:00', 'delivery', 1.50),
  (2, TIMESTAMP '2023-11-21 09:00:00', 'pickup', 2.00),
  (4, TIMESTAMP '2024-01-16 10:00:00', 'pickup', 3.25),
  (4, TIMESTAMP '2024-01-18 15:00:00', 'delivery', 3.25),
  (6, TIMESTAMP '2024-04-01 11:00:00', 'pickup', 0.75),
  (8, TIMESTAMP '2024-05-21 12:00:00', 'pickup', 1.00),
  (NULL, TIMESTAMP '2024-05-22 12:00:00', 'pickup', 0.50),
  (99, TIMESTAMP '2024-06-01 12:00:00', 'delivery', 4.00);

-- Order tags link orders to tags with no key: order 1's gift tag is stored twice, and order 9's
-- fragile tag is its only one.
CREATE TABLE tags (tag_id INTEGER, tag_name VARCHAR(16));
INSERT INTO tags VALUES (1, 'gift'), (2, 'rush'), (3, 'fragile');
CREATE TABLE order_tags (order_id INTEGER, tag_id INTEGER);
INSERT INTO order_tags VALUES (1, 1), (1, 1), (1, 2), (2, 2), (4, 1), (4, 3), (9, 3);

-- Rooms are stored with no key, one row per room, department and use. r1 is a design lab and
-- a finance office, r3's office row is stored twice (once without its floor), r4 has no
-- department, r5's two rows disagree on its area, and one row has no room. r1 and r3 have the
-- same area.
CREATE TABLE room_rows (
  room_id VARCHAR(8), building VARCHAR(8), floor INTEGER, area INTEGER,
  department VARCHAR(16), room_use VARCHAR(16)
);
INSERT INTO room_rows VALUES
  ('r1', 'north', 1, 100, 'design', 'lab'),
  ('r1', 'north', 1, 100, 'finance', 'office'),
  ('r2', 'north', 2, 300, 'design', 'office'),
  ('r3', 'south', 1, 100, 'finance', 'office'),
  ('r3', 'south', NULL, 100, 'finance', 'office'),
  ('r4', 'south', 2, 50, NULL, 'storage'),
  ('r5', 'south', 3, 80, 'legal', 'office'),
  ('r5', 'south', 3, 90, 'legal', 'lab'),
  (NULL, 'north', 9, 999, 'design', 'office');
-- People sit in rooms: two in r1 on equal pay and one in r2 on more, one each in r3 and r4,
-- one in a room no row has (r9) and one with no room.
CREATE TABLE people (
  person_id INTEGER, room_id VARCHAR(8), title VARCHAR(16), salary INTEGER, started_on DATE
);
INSERT INTO people VALUES
  (1, 'r1', 'engineer', 100, DATE '2024-01-10'),
  (2, 'r1', 'engineer', 100, DATE '2024-02-10'),
  (3, 'r2', 'engineer', 400, DATE '2024-02-20'),
  (4, 'r3', 'analyst', 250, DATE '2024-03-05'),
  (5, 'r4', 'clerk', 60, DATE '2024-03-15'),
  (6, 'r9', 'analyst', 50, DATE '2024-04-01'),
  (7, NULL, 'clerk', 70, DATE '2024-04-20');
-- Room bookings, with no key: ana's r1 booking is stored twice, one booking's room no row has
-- (r9), and one has no room.
CREATE TABLE room_bookings (room_id VARCHAR(8), booker VARCHAR(16));
INSERT INTO room_bookings VALUES
  ('r1', 'ana'), ('r1', 'ana'), ('r2', 'ana'), ('r3', 'ben'), ('r5', 'cy'), ('r9', 'dee'),
  (NULL, 'eve');

-- Daily seats per account, a stock. In the week of 2024-01-01 account 1 is on plan builder
-- Monday to Wednesday and pro from Thursday, account 2 is pro Monday and Tuesday and builder
-- from Wednesday, and account 3's last snapshot is Friday's. Nobody is observed the next week.
CREATE TABLE account_days (account_id INTEGER, snapshot_day DATE, plan VARCHAR(8), seats INTEGER);
INSERT INTO account_days VALUES
  (1, DATE '2024-01-01', 'builder', 10), (1, DATE '2024-01-02', 'builder', 11),
  (1, DATE '2024-01-03', 'builder', 12), (1, DATE '2024-01-04', 'pro', 13),
  (1, DATE '2024-01-05', 'pro', 14), (1, DATE '2024-01-06', 'pro', 15),
  (1, DATE '2024-01-07', 'pro', 16),
  (2, DATE '2024-01-01', 'pro', 1000), (2, DATE '2024-01-02', 'pro', 1001),
  (2, DATE '2024-01-03', 'builder', 1002), (2, DATE '2024-01-04', 'builder', 1003),
  (2, DATE '2024-01-05', 'builder', 1004), (2, DATE '2024-01-06', 'builder', 1005),
  (2, DATE '2024-01-07', 'builder', 1006),
  (3, DATE '2024-01-01', 'team', 100), (3, DATE '2024-01-02', 'team', 100),
  (3, DATE '2024-01-03', 'team', 100), (3, DATE '2024-01-04', 'team', 100),
  (3, DATE '2024-01-05', 'team', 100),
  (1, DATE '2024-01-15', 'pro', 20), (3, DATE '2024-01-15', 'team', 200);

-- Support tickets, a quiet event table: tickets open on 2024-09-02, 09-04 and 09-11, and a
-- stray ticket 4 lands on 2024-10-09, after the last load. The nightly load log ticket_loads
-- records each run's loaded-through day, the last through Sunday 2024-09-29; ticket_audits,
-- a log no run has written to yet, is empty.
CREATE TABLE tickets (ticket_id INTEGER, opened_at TIMESTAMP, minutes INTEGER);
INSERT INTO tickets VALUES
  (1, TIMESTAMP '2024-09-02 09:00:00', 30), (2, TIMESTAMP '2024-09-04 14:00:00', 45),
  (3, TIMESTAMP '2024-09-11 10:00:00', 20), (4, TIMESTAMP '2024-10-09 08:00:00', 15);
CREATE TABLE ticket_loads (run_id INTEGER, through_day DATE);
INSERT INTO ticket_loads VALUES
  (1, DATE '2024-09-15'), (2, DATE '2024-09-22'), (3, DATE '2024-09-29');
CREATE TABLE ticket_audits (run_id INTEGER, through_day DATE);
-- ticket_exports, a nightly that ran through Thursday 2024-11-14: November has no ticket.
CREATE TABLE ticket_exports (run_id INTEGER, through_day DATE);
INSERT INTO ticket_exports VALUES (1, DATE '2024-11-13'), (2, DATE '2024-11-14');

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
