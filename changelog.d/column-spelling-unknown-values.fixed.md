- On DuckDB, MotherDuck, DuckLake, BigQuery, Databricks and Athena, which read a quoted column
  name in any letter case as one column, a comparison word on a column (`is`, `not`,
  `missing`, a text word such as `contains`) in an `aggregate_if` condition or a declared
  measure is now refused with `INVALID_QUERY` when two dimensions over that column, spelled in
  different cases, declare different `unknown_values`, as it already was for two dimensions
  over one spelling. It answered with whichever dimension matched the spelling, so
  `"Customer Ref"` and `"CUSTOMER REF"` gave two totals for one column. A text word now also
  names "several dimensions" there when they agree; a comparison word still answers. Filter on
  the dimension you mean with `where`.
