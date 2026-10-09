- A limited ranking no longer ranks a NULL value first: descending on Postgres or
  Snowflake, or ascending on BigQuery or Databricks. Requested `order_by` terms now
  sort NULLs last in both directions on every backend; a rank by withheld values
  keeps its documented order.
