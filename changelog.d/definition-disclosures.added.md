- A query with no `time` block that selects measures of different entities or governed metrics
  with differing sets of real time roles, mixing at least two distinct roles, now carries one
  `MIXED_TIME_ROLES` warning naming those roles. Each period is read on its own role's clock;
  measure-level filters can bound those periods. Undated measures are ignored. A governed
  metric counts as one clock, and measures that share a role never warn. The SQL and the rows
  are unchanged. See [What an answer covers](docs/QUERY_IR_SCHEMA.md#what-an-answer-covers).
