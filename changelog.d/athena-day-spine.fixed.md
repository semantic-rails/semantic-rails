- Athena: queries that fill a time window, such as a prior-period or rolling comparison, failed
  with `FUNCTION_NOT_FOUND` because Athena has no `SEQUENCE(date, date)`. The implicit calendar
  now counts whole days with `SEQUENCE(0, DATE_DIFF('day', start, end))` and adds each to the
  start date, so every day is still a `DATE`.
