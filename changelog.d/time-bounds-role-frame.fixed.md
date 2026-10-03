- Resolve relative time ranges using the temporal role's local date, so equivalent
  UTC and offset timestamps produce the same bounds. Refuse coarse relative periods
  on non-default calendars; use exact dates for those periods.
- Apply the same whole-day bounds to DATE clocks and calendar fill, including an
  end day when the exclusive end falls after midnight, after timezone conversion
  and when a snapshot or population clock differs from the query's time axis.
- Apply entity-only predicate windows using valid logical field filters on
  unconverted roles. Refuse roles requiring timezone conversion rather than
  comparing local bounds against raw stored values.
