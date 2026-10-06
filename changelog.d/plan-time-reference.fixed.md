- Planning uses the caller's `policy_context.now` for current date and calendar windows,
  consistently with relative ranges. As-of requests such as "right now" and "end of last
  month" are held with `TIME_WINDOW_UNRESOLVED` instead of drafting an ordinary interval.
