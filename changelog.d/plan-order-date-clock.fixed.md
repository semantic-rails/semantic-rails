- `plan` and `ask` read a grouping that names the query's own clock as its
  time axis when they total a measure or metric by dimensions. "Revenue by
  store and order date at month grain, from January 1 2017 to March 31 2017"
  grouped by *Customer first order at* as well as the month, and still
  reported `ok`; it now groups by store and month only. "Revenue by store by
  order date" dropped the date and returned one row per store; it now returns
  one row per store and day, and a cadence the question names ("monthly",
  "at week grain") sets the buckets instead.
