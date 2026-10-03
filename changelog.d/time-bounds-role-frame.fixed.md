- Resolve relative time ranges using the temporal role's local date, so equivalent
  UTC and offset timestamps produce the same bounds. Refuse coarse relative periods
  on non-default calendars; use exact dates for those periods.
- Apply the same whole-day bounds to DATE clocks and calendar fill, including an
  end day when the exclusive end falls after midnight.
