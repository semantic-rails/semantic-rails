- `plan` keeps calendar windows it used to drop silently, such as "in 2017", "for 2017", "the first
  half of 2017", "Q2 of 2017", "April 1 to April 7, 2017" and ISO dates, and gives a total over
  one of them a grain that yields one bucket. It resolves a window only when the question names
  exactly one, in a form it reads unambiguously. A bound ("before 2017", "since March 2017"), a
  qualifier ("early 2017", "the end of 2017"), a comparison year, a numeric date such as
  4/3/2017, or two windows at once is reported as `TIME_WINDOW_UNRESOLVED`, even when the draft
  carries another window, instead of being narrowed or widened to the nearest form that parses.
- For a metric that looks back over earlier periods (month-over-month growth, rolling or
  cumulative totals), the engine can't bound `time.start`, so `plan` keeps the window's end and
  reports `TIME_WINDOW_START_DROPPED` with the start to filter the rows by, instead of returning
  a draft that fails validation.
