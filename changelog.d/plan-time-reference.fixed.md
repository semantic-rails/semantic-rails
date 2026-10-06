- Planning uses the caller's `policy_context.now` for current date, calendar and relative
  windows, drafting and checking them in one planning zone (the package default, else UTC).
  A question whose window reads different days in the selected temporal role's zone is held
  with `TIME_WINDOW_UNRESOLVED` and returns no query. A returned relative range carries the
  bounds that clock gives it, and a caller bound whose offset isn't the role zone's no longer
  matches the question by its written date. As-of requests such as "right now", "end of last
  month", or "2026-01-01 to now" are held with `TIME_WINDOW_UNRESOLVED`; unrepresentable
  closing-day bounds return the same hold.
