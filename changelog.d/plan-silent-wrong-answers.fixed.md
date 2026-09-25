- `plan` no longer returns a runnable `best.query_ir` when it can't resolve the question's time
  window (`TIME_WINDOW_UNRESOLVED`); executing that draft used to return every period with `ok`.
  Pass the window in `query.time` and plan again.
- A number in a time phrase no longer makes a top N: "What was revenue from January 1 2017 to
  March 31 2017 by store?" or "…in the last 3 months by store?" used to return only the first
  1 or 3 rows ranked by revenue.
- A question that names a metric whose measure has the same name ("rolling 28-day revenue by
  day", "Revenue QTD by day") now uses that metric instead of plain revenue.
- `plan` flags a second subject named with "count" or a question word ("What is order count and
  revenue by month?", "how many orders and revenue by month") instead of answering with revenue
  alone.
- `ask` and the REPL now print the plan's own warnings, such as `PLAN_UNMATCHED_TERMS`.
- The `UNGRAINED_TIME_PROJECTION` hint no longer suggests removing `time.temporal_role`, which
  the engine rejects. The MCP `max_rows` description says `total_row_count` is null past
  10,000 rows.
