# Data Provenance

The CSVs under `data/jaffle_csv/` (and the derived `jaffle_shop.duckdb` /
`seed_jaffle.sql` fixtures) are synthetic demo data generated independently
for this repository. All customer, order, session, inventory, and store rows
are fabricated — they contain no real-world records.

The dataset concept and schema follow the **Jaffle Shop** example created by
[dbt Labs](https://github.com/dbt-labs/jaffle-shop), whose jaffle-shop
repositories are Apache-2.0 licensed. We use the same fictional-cafe framing
and a compatible table layout (`raw_customers`, `raw_orders`, `raw_items`,
`raw_products`, `raw_stores`, `raw_supplies`) so the proof package reads
familiarly to anyone who knows the dbt tutorial, plus additional tables this
repo adds for its own primitives (`raw_customer_history`,
`raw_order_lifecycle_events`, `raw_storefront_sessions`,
`raw_store_inventory_snapshots`, `raw_calendar_fiscal`). Some small reference
values (for example, whimsical product names in `raw_products.csv`) follow the
upstream example's catalog; the row-level transactional data is our own
generation, and we do not redistribute dbt Labs' seed files.

Credit: Jaffle Shop concept and schema — dbt Labs (Apache-2.0). dbt Labs is
not affiliated with and does not endorse this project.
