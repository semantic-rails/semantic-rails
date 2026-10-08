- `plan` answers a balance question on one day. When every select reads a stock on its
  `as_of_time` clock, a question that names no day, or says "now", "right now" or "current",
  reads the last complete day before `policy_context.now`; "at the end of last month", "as of
  2026-09-30" or one stated period ("MRR last month") reads that period's closing day. The
  response's `assumptions` names the day. A day that isn't complete is never drafted and no
  earlier day stands in for one, so an unloaded day returns no rows. A `metric_constraint` that
  requires the clock's date dimension in `group_by` now shapes the draft instead of denying it,
  and a balance compared across days, or asked by week or month where it is read per day,
  returns `needs_clarification`. See "How plan reads a balance" in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md#plan).
