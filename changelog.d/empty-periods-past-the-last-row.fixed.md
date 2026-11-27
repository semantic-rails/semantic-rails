- On Snowflake, BigQuery, Databricks and Athena, a measure whose table declares no
  `zero_through` now reads `NULL`, not `0`, in a period past its own last row (or before its
  first) that another measure's rows put in the answer, as DuckDB and Postgres already did.
  Periods past the last row also get `NO_DATA_YET`. "Orders and tickets, by month" read `0`
  orders in September and October 2024 though the last order is dated August 1, and orders per
  ticket read `0.0` there. Both now read `NULL`, and a sum or difference of two such measures
  reads `NULL` there too. This holds with or without a window and with `fill`, and it fixes the
  known limit listed under 0.3.8 except inside a joined call (`parts`): a part that selects
  measures from two tables still reads `0` there when the call has no `start`. Ask for those
  measures in a call of their own, or declare `zero_through`. Empty periods between a measure's
  own first and last row still read `0`. To tell them apart, such a series now reads each measure's
  own table on those warehouses, not a rollup, as DuckDB and Postgres do. One number moves the
  other way: in such a series over an open window, a measure the monthly rollup answered read
  `NULL` in a month with none of its rows, even when it had rows before the window; it now reads
  `0` there, as DuckDB and Postgres do and as it did when the rollup couldn't answer.
- A package can use a table named `coverage_1` in a query that checks when its data was loaded.
