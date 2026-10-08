- Refuse filled series and non-default calendar grouping when an authored calendar's
  requested period column or `date_day` column is not declared as a date,
  or `date_day` is not the calendar entity's declared single-column key,
  naming the missing declaration before execution with
  `calendar_day_key_unproven` for an unproven day or day key.
- Default-calendar fills now use the engine's `DATE_TRUNC` buckets at every
  grain, matching populated leaf buckets even with physical timestamp anchors.
  Weeks start on ISO Monday; a default calendar with Sunday `week_start`
  previously returned silent NULLs and now returns ISO weeks. Non-ISO weeks
  require a non-default calendar. A `TIMESTAMPTZ` `date_day` built in another
  time zone is unsupported.
- Non-default fills refuse with `calendar_leaf_unbound` when the leaf cannot
  bind the authored calendar bucket, rather than losing populated buckets.
  Calendar joins compare both day keys as dates, retaining counts for noon
  timestamp day keys behind the declared day-key preconditions.
