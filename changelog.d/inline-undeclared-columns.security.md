- Under a metric constraint with `allowed_where` (or `allow_metric_filters: false`), a column no
  dimension declares is refused wherever an `aggregate_if` value computes with it, not only in
  a condition, with the same `POLICY_DENIED` and `disallowed_where` violation, naming
  `<entity>.<column>`. Listing that name in `allowed_where` allows nothing, even when a
  dimension's id reads the same. Ordinary arithmetic is refused too, such as
  `sum(order_total_cents / 100.0)` or a total net of tax: the refusal says the package author
  can declare a dimension over the column and list it in `allowed_where` if the role may use
  it. A `count`, `count_distinct` or `avg` of a column counts only the rows where that column
  has a value, so it is held as a filter on that column unless the column is its entity's key
  or the column the governed measure itself aggregates with no expression around it: a measure
  over `coalesce(customer_id, 'guest')` or `order_total_cents / 100.0` exempts nothing. Each
  aggregate is held to its own governed measures, so two averages that answer alone also
  answer together. A dimension allows a column on another entity only over the same relation,
  named in full. A column spelled in another case meets every dimension over it wherever the
  warehouse reads the spellings as one column (a quoted name on DuckDB, BigQuery, Databricks
  and Athena). Callers no constraint applies to get the same answers as before.
