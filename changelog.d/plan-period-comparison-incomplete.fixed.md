- `plan` no longer calls a period comparison ready when a period it returns hasn't
  ended at the request's `now`. "revenue month over month" used to put the month so
  far beside the whole previous month; a comparison through a `prior_period`
  expression, or a metric built on one, now returns `low_confidence` with
  `PERIOD_COMPARISON_INCOMPLETE`, which names the incomplete period and offers the
  complete periods (`query.time.end` on the last period end). A matched package
  example's authored query is held the same way. A window that has
  ended stays as it was. The hint of `TIME_WINDOW_START_DROPPED` no longer asks to
  execute a comparison that returns an incomplete period.
