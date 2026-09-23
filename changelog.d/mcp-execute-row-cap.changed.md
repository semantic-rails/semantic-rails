- MCP `execute` returns at most `max_rows` rows (default 200). A larger result comes back with
  `truncated: true`, `total_row_count` and an `EXECUTE_ROWS_TRUNCATED` warning; pass a larger
  `max_rows` (up to 100,000) to see more. A `limits.max_rows` in the query can lower the cap but
  never raises it. The HTTP API doesn't cap rows.
- MCP `validate`, `compile` and `execute` warn with `UNGRAINED_GROUPED_TIME_PROJECTION` when a
  grouped query has a temporal role but no grain, like the runtime's `UNGRAINED_TIME_PROJECTION`
  does for ungrouped queries.
