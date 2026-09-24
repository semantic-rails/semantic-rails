- MCP `execute` can bound its response with an explicit `max_rows` (for example, 200; maximum
  100,000). A larger result returns `truncated: true`, `total_row_count` and an
  `EXECUTE_ROWS_TRUNCATED` warning. An unchanged v1 call keeps its prior uncapped behavior and
  the query's own `limits.max_rows`; the HTTP API also does not add a response cap.
- MCP `validate`, `compile` and `execute` warn with `UNGRAINED_GROUPED_TIME_PROJECTION` when a
  grouped query has a temporal role but no grain, like the runtime's `UNGRAINED_TIME_PROJECTION`
  does for ungrouped queries.
