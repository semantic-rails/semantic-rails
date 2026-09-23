- MCP `execute` returns at most `max_rows` rows (default 200). A larger result comes back with
  `truncated: true`, `total_row_count` and an `EXECUTE_ROWS_TRUNCATED` warning; pass a larger
  `max_rows` to see more. The HTTP API doesn't cap rows.
- MCP `validate`, `compile` and `execute` also warn with `UNGRAINED_TIME_PROJECTION` when a
  grouped query has a time window but no grain.
