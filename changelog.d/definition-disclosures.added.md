- A query with no `time` block that selects measures of two or more entities dated
  by different time roles (orders by order time and storefront sessions by session start,
  grouped by customer) now carries one `MIXED_TIME_ROLES` warning naming each measure's role:
  each covers all of its own history, so a ratio of them is not a rate over one period. A
  metric counts as one clock, and measures that share a role never warn. The SQL and the rows
  are unchanged. See [What an answer covers](docs/QUERY_IR_SCHEMA.md#what-an-answer-covers).
