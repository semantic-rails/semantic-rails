-- Portable seed: DuckDB and Postgres run this same script, and comments here hold no semicolons.
-- HR and facilities both carry a department code. Engineering is two people, both named Alex.
-- Two people have no department and one is filed under UNASSIGNED, a code no department has.
-- Marketing has people and no space, Legal space and no people, and 900 sq ft is assigned to
-- no department.
CREATE TABLE departments (dept_code VARCHAR(12), dept_name VARCHAR(24));
INSERT INTO departments VALUES ('ENG', 'Engineering'), ('FIN', 'Finance'),
  ('OPS', 'Operations'), ('LAW', 'Legal'), ('MKT', 'Marketing'), ('RES', 'Research');

CREATE TABLE employees (
  employee_id INTEGER, employee_name VARCHAR(16), dept_code VARCHAR(12), site VARCHAR(8),
  salary INTEGER
);
INSERT INTO employees VALUES
  (1, 'Alex', 'ENG', 'north', 100), (2, 'Alex', 'ENG', 'south', 120),
  (3, 'Bea', 'OPS', 'north', 90), (4, 'Cal', 'FIN', 'north', 80),
  (5, 'Dee', 'OPS', 'south', 70), (6, 'Eli', 'OPS', 'south', 75),
  (7, 'Ivy', 'MKT', 'north', 60), (8, 'Fay', NULL, 'north', 50),
  (9, 'Gus', NULL, 'south', 55), (10, 'Hal', 'UNASSIGNED', 'north', 65);

CREATE TABLE space (space_id INTEGER, building_key INTEGER, dept_code VARCHAR(12),
  area_sqft INTEGER);
INSERT INTO space VALUES
  (1, 32, 'ENG', 500), (2, 32, 'FIN', 300), (3, 40, 'ENG', 200), (4, 40, 'OPS', 400),
  (5, 40, 'LAW', 100), (6, 41, NULL, 900);

CREATE TABLE trainings (training_id INTEGER, employee_id INTEGER);
INSERT INTO trainings VALUES (1, 1), (2, 1), (3, 3), (4, 4), (5, 8);

CREATE TABLE rooms (room_key VARCHAR(8));
INSERT INTO rooms VALUES ('R1'), ('R2'), ('R3');

CREATE TABLE work_orders (work_order_id INTEGER, room_key VARCHAR(8), opened_at TIMESTAMP);
INSERT INTO work_orders VALUES
  (1, 'R1', TIMESTAMP '2024-01-05 09:00:00'), (2, 'R1', TIMESTAMP '2024-02-07 09:00:00'),
  (3, 'R2', TIMESTAMP '2024-01-20 09:00:00'), (4, 'R2', TIMESTAMP '2024-02-21 09:00:00'),
  (5, 'R2', TIMESTAMP '2024-02-25 09:00:00');

CREATE TABLE reservations (reservation_id INTEGER, room_key VARCHAR(8), reserved_at TIMESTAMP);
INSERT INTO reservations VALUES
  (1, 'R1', TIMESTAMP '2024-01-10 10:00:00'), (2, 'R2', TIMESTAMP '2024-02-11 10:00:00'),
  (3, 'R1', TIMESTAMP '2024-03-12 10:00:00'), (4, 'R3', TIMESTAMP '2024-03-15 10:00:00');
