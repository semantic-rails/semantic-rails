-- Portable seed: DuckDB and Postgres run this same script, and comments here hold no semicolons.
-- 236 vendors named Vendor 001 to Vendor 236, so a name sorts where its number does, and one
-- vendor with no name.
CREATE TABLE vendors AS
SELECT g.i AS vendor_id, 'Vendor ' || LPAD(CAST(g.i AS VARCHAR), 3, '0') AS vendor_name
FROM generate_series(1, 236) AS g(i);
INSERT INTO vendors VALUES (237, NULL);
-- 236 products, each by a code: JAF-002 for an even number, SES-001 for an odd one.
CREATE TABLE products AS
SELECT CASE WHEN g.i % 2 = 0 THEN 'JAF-' ELSE 'SES-' END || LPAD(CAST(g.i AS VARCHAR), 3, '0')
  AS sku
FROM generate_series(1, 236) AS g(i);
-- 236 shipments, each by a UUID (the md5 of its number) in the warehouse's own UUID type.
CREATE TABLE shipments AS
SELECT CAST(md5(CAST(g.i AS VARCHAR)) AS UUID) AS shipment_id
FROM generate_series(1, 236) AS g(i);
