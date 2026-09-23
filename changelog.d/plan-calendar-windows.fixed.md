- `plan` keeps calendar windows it used to drop silently: years ("in 2017"), half years ("first
  half of 2017") and days ("April 1 to April 7, 2017"). A total over such a window gets a grain
  that yields one bucket, instead of grouping by the raw timestamp. A year-over-year comparison
  ("2017 vs 2016") is reported as an unresolved window (`TIME_WINDOW_UNRESOLVED`) rather than
  narrowed to one year.
