- On Snowflake, BigQuery, Databricks and Athena, a measure whose table declares no
  `zero_through` now reads `NULL`, not `0`, in a period past its own last row (or before its
  first) that another measure's rows put in the answer, as DuckDB and Postgres already did.
  Periods past the last row also get `NO_DATA_YET`. "Orders and tickets, by month" read `0`
  orders in September and October 2024 though the last order is dated August 1, and orders per
  ticket read `0.0` there; both now read `NULL`. This holds with or without a window and with
  `fill`, and it fixes the known limit listed under 0.3.8. Empty periods between a measure's own
  first and last row still read `0`. To tell them apart, such a series now reads each measure's
  own table on those warehouses, not a rollup, as DuckDB and Postgres do.
