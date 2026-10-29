-- Portable seed: DuckDB and Postgres run this same script, and comments here hold no semicolons.
-- A room names its building by its own key and its floor by another: R-202 names B-2 but sits
-- on a floor of B-1, so the two routes from a room to its building read different buildings
-- for it. R-103 and R-105 name no building and no floor, so the offices mix rooms with and
-- without a building. B-3 has no room. room_rows stores the rooms again without a key, R-101
-- twice. B-1 has addresses in two cities, two of them in Springfield. B-2 has none. B-3's
-- address is in Shelbyville.
CREATE TABLE buildings (
  building_id INTEGER, building_name VARCHAR(8), campus VARCHAR(8), assignable_area INTEGER,
  occupants INTEGER
);
INSERT INTO buildings VALUES
  (1, 'B-1', 'North', 1000, 120), (2, 'B-2', 'South', 600, 80), (3, 'B-3', 'North', 400, 30);

CREATE TABLE floors (floor_id INTEGER, building_id INTEGER, floor_name VARCHAR(8));
INSERT INTO floors VALUES (11, 1, 'B-1 F1'), (12, 1, 'B-1 F2'), (21, 2, 'B-2 F1'), (31, 3, 'B-3 F1');

CREATE TABLE organizations (org_id INTEGER, org_name VARCHAR(16));
INSERT INTO organizations VALUES (1, 'Physics'), (2, 'History'), (3, 'Music');

CREATE TABLE rooms (
  room_id INTEGER, room_code VARCHAR(8), building_id INTEGER, floor_id INTEGER, org_id INTEGER,
  room_use VARCHAR(8), area INTEGER
);
INSERT INTO rooms VALUES
  (1, 'R-101', 1, 11, 1, 'lab', 30), (2, 'R-102', 1, 12, 1, 'office', 20),
  (3, 'R-104', 1, 11, 1, 'lab', 15), (4, 'R-201', 2, 21, 2, 'office', 50),
  (5, 'R-202', 2, 12, 2, 'seminar', 40), (6, 'R-103', NULL, NULL, 1, 'storage', 10),
  (7, 'R-105', NULL, NULL, 2, 'office', 25);

CREATE TABLE room_rows (
  room_code VARCHAR(8), building_id INTEGER, org_id INTEGER, room_use VARCHAR(8), area INTEGER
);
INSERT INTO room_rows VALUES
  ('R-101', 1, 1, 'lab', 30), ('R-101', 1, 1, 'lab', 30), ('R-102', 1, 1, 'office', 20),
  ('R-104', 1, 1, 'lab', 15), ('R-201', 2, 2, 'office', 50), ('R-202', 2, 2, 'seminar', 40),
  ('R-103', NULL, 1, 'storage', 10), ('R-105', NULL, 2, 'office', 25);

CREATE TABLE addresses (
  address_id INTEGER, building_id INTEGER, city VARCHAR(16), street VARCHAR(16)
);
INSERT INTO addresses VALUES
  (1, 1, 'Springfield', '1 Main St'), (2, 1, 'Springfield', '3 Main St'),
  (3, 1, 'Shelbyville', '9 Elm St'), (4, 3, 'Shelbyville', '4 Oak St');
