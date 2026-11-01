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

-- Each room's seats, which have no month, and its wing. R4 has no work orders or reservations.
CREATE TABLE rooms (room_key VARCHAR(8), wing VARCHAR(8), seats INTEGER);
INSERT INTO rooms VALUES ('R1', 'east', 10), ('R2', 'east', 20), ('R3', 'west', 6),
  ('R4', 'west', 8);

CREATE TABLE work_orders (work_order_id INTEGER, room_key VARCHAR(8), opened_at TIMESTAMP);
INSERT INTO work_orders VALUES
  (1, 'R1', TIMESTAMP '2024-01-05 09:00:00'), (2, 'R1', TIMESTAMP '2024-02-07 09:00:00'),
  (3, 'R2', TIMESTAMP '2024-01-20 09:00:00'), (4, 'R2', TIMESTAMP '2024-02-21 09:00:00'),
  (5, 'R2', TIMESTAMP '2024-02-25 09:00:00');

CREATE TABLE reservations (reservation_id INTEGER, room_key VARCHAR(8), reserved_at TIMESTAMP);
INSERT INTO reservations VALUES
  (1, 'R1', TIMESTAMP '2024-01-10 10:00:00'), (2, 'R2', TIMESTAMP '2024-02-11 10:00:00'),
  (3, 'R1', TIMESTAMP '2024-03-12 10:00:00'), (4, 'R3', TIMESTAMP '2024-03-15 10:00:00');

-- Facilities' own assignments, keyed by building and department code, with no relationship to
-- the departments model: LAB is a code HR doesn't have, and Marketing and Finance have none.
CREATE TABLE assignments (building_key INTEGER, dept_code VARCHAR(12), area_sqft INTEGER);
INSERT INTO assignments VALUES
  (32, 'ENG', 500), (40, 'ENG', 200), (40, 'OPS', 400), (40, 'LAW', 100), (41, 'LAB', 250);

-- Mailing lists and their members, a membership row per list and person with a role. A list has
-- several owners (L1), one (L2) or none (L3), and lists share members (c is on L1 and L2).
CREATE TABLE lists (list_key VARCHAR(8), list_name VARCHAR(16));
INSERT INTO lists VALUES ('L1', 'Staff'), ('L2', 'Labs'), ('L3', 'Alumni');

CREATE TABLE list_members (list_key VARCHAR(8), member_email VARCHAR(16), member_role VARCHAR(8));
INSERT INTO list_members VALUES
  ('L1', 'a@x', 'owner'), ('L1', 'b@x', 'owner'), ('L1', 'c@x', 'member'),
  ('L2', 'c@x', 'owner'), ('L2', 'd@x', 'member'), ('L3', 'e@x', 'member');

-- Instructors keyed by key and name: I1 is filed under two names. Course reserves hold the
-- key only: I9 is no instructor listed, and one reserve names none.
CREATE TABLE instructors (instructor_key VARCHAR(8), instructor_name VARCHAR(16));
INSERT INTO instructors VALUES ('I1', 'Ann Lee'), ('I1', 'Ann Li'), ('I2', 'Bo Chu'),
  ('I3', 'Cy Diaz');

CREATE TABLE course_reserves (reserve_id INTEGER, instructor_key VARCHAR(8));
INSERT INTO course_reserves VALUES (1, 'I1'), (2, 'I1'), (3, 'I1'), (4, 'I2'), (5, 'I9'),
  (6, 'I9'), (7, NULL);
