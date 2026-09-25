-- A tiny shop warehouse for the starter scenarios: 4 customers, 8 orders.
CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR, country VARCHAR, signed_up_at DATE);
INSERT INTO customers VALUES
  (1, 'Ada', 'UK', '2026-01-04'), (2, 'Bo', 'US', '2026-01-19'),
  (3, 'Chen', 'US', '2026-02-02'), (4, 'Dara', 'IE', '2026-02-20');
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers (customer_id),
  ordered_at TIMESTAMP, status VARCHAR, amount DECIMAL(10, 2)
);
INSERT INTO orders VALUES
  (101, 1, '2026-03-01 10:00', 'completed', 40.00), (102, 2, '2026-03-03 12:30', 'completed', 25.50),
  (103, 2, '2026-03-09 09:15', 'returned', 12.00), (104, 3, '2026-03-15 18:45', 'completed', 60.00),
  (105, 4, '2026-03-21 08:05', 'cancelled', 30.00), (106, 1, '2026-04-02 14:20', 'completed', 15.25),
  (107, 3, '2026-04-11 16:00', 'completed', 80.00), (108, 4, '2026-04-18 11:10', 'returned', 22.00);
