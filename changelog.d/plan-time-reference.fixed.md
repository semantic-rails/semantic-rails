- Planning uses the caller's `policy_context.now` in the selected temporal role's zone for
  current date and calendar windows, consistently with relative ranges. Mismatched drafts
  and as-of requests such as "right now", "end of last month", or "2026-01-01 to now" are
  held with `TIME_WINDOW_UNRESOLVED`; unrepresentable closing-day bounds return the same hold.
