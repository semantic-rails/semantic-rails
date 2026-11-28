- On DuckDB, MotherDuck, DuckLake, Databricks and Athena, which read a quoted column
  name in any ASCII letter case as one column, a comparison word on a column (`is`, `not`,
  `missing`, a text word such as `contains`) in an `aggregate_if` condition or a declared
  measure is now refused with `INVALID_QUERY` when two dimensions over that column, spelled in
  different cases, declare different `unknown_values`, as it already was for two dimensions
  over one spelling. On BigQuery this also applies to quoted names it keeps as written, such as
  `Select` and `select`. Names it rewrites, such as `Customer Ref` and `customer ref`, become
  separate columns. On every warehouse but ClickHouse the same holds for two unquoted
  spellings such as `ref_code` and `REF_CODE`. Previously, the comparison used whichever
  dimension matched the spelling, so `"Customer Ref"` and `"CUSTOMER REF"` gave two totals
  for one column. A text word now also names "several dimensions" there when they agree.
  A comparison word still answers. Filter on the dimension you mean with `where`.
