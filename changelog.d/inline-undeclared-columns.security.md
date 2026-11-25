- Under a metric constraint with `allowed_where` (or `allow_metric_filters: false`), a column no
  dimension declares is refused wherever an `aggregate_if` value computes with it, not only in
  a condition, with the same `POLICY_DENIED` and `disallowed_where` violation, naming
  `<entity>.<column>`. Ordinary arithmetic is refused too, such as
  `sum(order_total_cents / 100.0)` or a total net of tax: the refusal says the package author
  can declare a dimension over the column and list it in `allowed_where` if the role may use
  it. A `count`, `count_distinct` or `avg` of a column other than its entity's key or a column
  the governed measure reads counts only the rows where that column has a value, so it is held
  as a filter on that column. A column spelled in another case meets the dimension over it
  wherever the warehouse reads the two spellings as one column (a quoted name on DuckDB,
  BigQuery, Databricks and Athena). Callers no constraint applies to get the same answers as
  before.
