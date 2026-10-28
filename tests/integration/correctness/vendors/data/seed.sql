-- Portable seed: DuckDB and Postgres run this same script, and comments here hold no semicolons.
-- 236 vendors named Vendor 001 to Vendor 236, so a name sorts where its number does, and one
-- vendor with no name.
CREATE TABLE vendors AS
SELECT g.i AS vendor_id, 'Vendor ' || LPAD(CAST(g.i AS VARCHAR), 3, '0') AS vendor_name
FROM generate_series(1, 236) AS g(i);
INSERT INTO vendors VALUES (237, NULL);
