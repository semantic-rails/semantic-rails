- The MCP `execute` tool refuses a result whose rows serialize to more than 32,000 characters
  (about 8,000 tokens) with the error `RESULT_TOO_LARGE`. No rows are returned; the message names
  the row count and what would fit (a set or coarser `time.grain`, a filter, fewer `group_by`
  dimensions or columns), and `details` carries `row_count`, `total_row_count`, `result_chars` and
  `max_result_chars`. Set another limit with `SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS`. The
  `max_rows` cap still clips long results and reports `truncated`. See
  [MCP interface](docs/MCP_INTERFACE.md).
