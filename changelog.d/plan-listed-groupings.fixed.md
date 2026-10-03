- `plan` reads a comma-separated grouping list ("by store, customer type", "by a, b, and c") as
  it reads one joined by "and", and doesn't call a draft ready to execute when it groups by fewer
  dimensions than the question lists groupings (clock terms and declared values aside), so two
  incidents that share a name are no longer added into one row. A grain phrase in the list ("by
  order date, at week grain", "by store and month level") sets the time grain of the measure's
  own clock instead of grouping by a calendar dimension.
