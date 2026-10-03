- A query with no window and no time grain that selects measures of two or more entities dated
  by different time roles (orders by order time and storefront sessions by session start,
  grouped by customer) now carries one `MIXED_TIME_ROLES` warning naming each measure's role:
  each covers all of its own history, so a ratio of them is not a rate over one period. A
  metric counts as one clock, and measures that share a role never warn. See
  [What an answer covers](docs/QUERY_IR_SCHEMA.md#what-an-answer-covers).
- An `avg`, `min`, `max`, `median` or `percentile` of a measure whose rows have a parent the
  output doesn't group by (item revenue grouped by customer, with orders in between) now says in
  `assumptions` which rows it runs over. For an `avg` the entry adds the per-parent average as a
  ratio over the package's count of that parent, ready to select. Neither change alters the SQL
  or the rows.
