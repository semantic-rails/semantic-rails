- `plan` answers a balance question on one day. When every select reads a stock on its
  `as_of_time` clock, a question that names no day, or says "now", "right now" or "current",
  reads the last complete day before `policy_context.now`; "at the end of last month", "as of
  2026-09-30" or one stated period ("MRR last month") reads that period's closing day. The
  response's `assumptions` names the day. Caller-selected expressions and aliases are preserved.
  Time words without a single-day reading, such as "all time" or "ever", stay held. A day that
  isn't complete stays held, and no earlier day stands in for one, so an unloaded complete
  day returns no rows. A day-grain balance window is read only when both bounds are whole days
  and it ends on or before the last complete day; otherwise `stock_as_of_unrealized`.
  A generated balance must name
  the governed subject in full; "pro accounts" cannot stand for "Paying accounts". A `metric_constraint` that
  requires the clock's date dimension in `group_by` now shapes the draft instead of denying it,
  and a balance compared across days, or asked by week or month where it is read per day,
  returns `needs_clarification`. See "How plan reads a balance" in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md#plan).
