-- A warehouse shaped like the output of `dbt build` on a dbt-duckdb project
-- whose target schema is `main`: seeds land in `main`, staging models are views
-- in `main_staging`, and marts are tables in `main_marts` (dbt's default
-- generate_schema_name gives `<target>_<custom>`). Tests build it from this
-- script, so the suite needs no dbt install. `dim_customers` carries the
-- constraints an enforced dbt model contract would create; the other marts,
-- like most dbt models, declare none.

CREATE SCHEMA main_staging;
CREATE SCHEMA main_marts;

CREATE TABLE main.raw_customers (id INTEGER, name VARCHAR, country VARCHAR, created_at DATE);
INSERT INTO main.raw_customers VALUES
  (1, 'Ada', 'GB', DATE '2024-01-05'),
  (2, 'Grace', 'US', DATE '2024-01-17'),
  (3, 'Edsger', 'NL', DATE '2024-02-02'),
  (4, 'Barbara', 'US', DATE '2024-02-20'),
  (5, 'Donald', 'US', DATE '2024-03-11');

CREATE TABLE main.raw_stores (id INTEGER, name VARCHAR, country VARCHAR, opened_on DATE, closed_on DATE);
INSERT INTO main.raw_stores VALUES
  (1, 'Online', 'US', DATE '2020-01-01', NULL),
  (2, 'Amsterdam', 'NL', DATE '2021-06-01', NULL),
  (3, 'London', 'GB', DATE '2019-03-01', DATE '2024-02-29');

CREATE TABLE main.raw_products (id INTEGER, name VARCHAR, category VARCHAR, unit_cost DECIMAL(10, 2), list_price DECIMAL(10, 2));
INSERT INTO main.raw_products VALUES
  (1, 'Keyboard', 'Accessories', 20.00, 45.00),
  (2, 'Monitor', 'Displays', 110.00, 199.00),
  (3, 'Mouse', 'Accessories', 8.00, 19.00),
  (4, 'Laptop', 'Computers', 700.00, 1199.00);

CREATE TABLE main.raw_orders (id INTEGER, customer_id INTEGER, store_id INTEGER, ordered_at TIMESTAMP, delivered_at TIMESTAMP, status VARCHAR);
INSERT INTO main.raw_orders VALUES
  (101, 1, 1, TIMESTAMP '2024-01-06 09:15:00', TIMESTAMP '2024-01-09 12:00:00', 'delivered'),
  (102, 2, 1, TIMESTAMP '2024-01-18 14:02:00', TIMESTAMP '2024-01-20 10:30:00', 'delivered'),
  (103, 1, 3, TIMESTAMP '2024-02-01 11:45:00', TIMESTAMP '2024-02-01 11:45:00', 'delivered'),
  (104, 3, 2, TIMESTAMP '2024-02-03 16:20:00', TIMESTAMP '2024-02-07 09:00:00', 'delivered'),
  (105, 4, 1, TIMESTAMP '2024-02-21 08:05:00', NULL, 'returned'),
  (106, 2, 2, TIMESTAMP '2024-03-02 13:30:00', TIMESTAMP '2024-03-05 15:10:00', 'delivered'),
  (107, 5, 1, TIMESTAMP '2024-03-12 19:55:00', NULL, 'shipped'),
  (108, 1, 1, TIMESTAMP '2024-03-20 07:40:00', NULL, 'placed');

CREATE TABLE main.raw_order_lines (order_id INTEGER, line_number INTEGER, product_id INTEGER, quantity INTEGER, unit_price DECIMAL(10, 2), discount DECIMAL(10, 2));
INSERT INTO main.raw_order_lines VALUES
  (101, 1, 1, 1, 45.00, 0.00),
  (101, 2, 3, 2, 19.00, 0.00),
  (102, 1, 2, 1, 199.00, 20.00),
  (103, 1, 4, 1, 1199.00, 100.00),
  (103, 2, 3, 1, 19.00, 0.00),
  (104, 1, 1, 2, 45.00, 5.00),
  (105, 1, 2, 1, 199.00, 0.00),
  (106, 1, 4, 1, 1199.00, 0.00),
  (106, 2, 1, 1, 45.00, 0.00),
  (106, 3, 3, 3, 19.00, 3.00),
  (107, 1, 2, 2, 199.00, 10.00),
  (108, 1, 3, 1, 19.00, 0.00);

CREATE VIEW main_staging.stg_customers AS
  SELECT id AS customer_id, name AS customer_name, country AS customer_country, created_at AS signed_up_on
  FROM main.raw_customers;
CREATE VIEW main_staging.stg_stores AS
  SELECT id AS store_id, name AS store_name, country AS store_country, opened_on, closed_on
  FROM main.raw_stores;
CREATE VIEW main_staging.stg_products AS
  SELECT id AS product_id, name AS product_name, category, unit_cost, list_price
  FROM main.raw_products;
CREATE VIEW main_staging.stg_orders AS
  SELECT id AS order_id, customer_id, store_id, ordered_at, delivered_at, status
  FROM main.raw_orders;
CREATE VIEW main_staging.stg_order_lines AS
  SELECT order_id, line_number, product_id, quantity, unit_price, discount,
         quantity * unit_price - discount AS net_amount
  FROM main.raw_order_lines;

CREATE TABLE main_marts.dim_customers (
  customer_id INTEGER PRIMARY KEY,
  customer_name VARCHAR NOT NULL,
  customer_country VARCHAR NOT NULL,
  signed_up_on DATE NOT NULL
);
INSERT INTO main_marts.dim_customers SELECT * FROM main_staging.stg_customers;

CREATE TABLE main_marts.dim_stores AS SELECT * FROM main_staging.stg_stores;
CREATE TABLE main_marts.dim_products AS SELECT * FROM main_staging.stg_products;

CREATE TABLE main_marts.fct_order_lines AS
  SELECT l.order_id, l.line_number, l.product_id, o.ordered_at, l.quantity, l.unit_price,
         l.discount, l.net_amount, l.quantity * p.unit_cost AS cost_amount
  FROM main_staging.stg_order_lines AS l
  JOIN main_staging.stg_orders AS o USING (order_id)
  JOIN main_staging.stg_products AS p USING (product_id);

CREATE TABLE main_marts.fct_orders AS
  SELECT o.order_id, o.customer_id, o.store_id, o.ordered_at, o.delivered_at, o.status,
         count(l.line_number) AS line_count, sum(l.net_amount) AS order_total
  FROM main_staging.stg_orders AS o
  JOIN main_staging.stg_order_lines AS l USING (order_id)
  GROUP BY ALL;
