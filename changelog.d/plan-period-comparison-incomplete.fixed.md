- `plan` no longer calls a period comparison ready when a period it returns hasn't
  ended at the request's `now`. "month over month revenue growth by month" used to put the
  month so far beside the whole previous month; a comparison through a `prior_period`
  expression, or a metric built on one, now returns `low_confidence` with
  `PERIOD_COMPARISON_INCOMPLETE`, which names the incomplete period and offers the
  complete periods (its option's call ends `time.end` on the last period end). A matched
  package example's authored call, and the granted-metric plan, are held the same way. The
  check reads every clock the draft buckets by (clocks in different time zones hold it), every
  bound that ends the window (a `where` upper bound on the clock's date counts like
  `time.end`), and holds a bound finer than a microsecond; the option ends before any period a
  filter cuts, and is offered only when it is complete. What it can't prove complete is held
  with no option: an entity's aggregate read beside the comparison, a measure's own filter on
  a date (in its definition or its model's relation pipeline), a bound with a non-UTC offset, or a comma before a fraction. A window that has ended stays as it
  was. The hint of `TIME_WINDOW_START_DROPPED` no longer
  asks to execute a comparison that returns an incomplete period.
