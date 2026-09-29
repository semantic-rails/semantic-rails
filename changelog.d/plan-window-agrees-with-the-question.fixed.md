- `plan` no longer calls a draft ready when its time window is not the one the question states.
  A window in the draft (one you pass in `query.time`, or plan's own) consumes the date phrases
  plan resolved only if it agrees with them: each bound it carries, read at the day, is the
  earliest start or the latest end of the windows the question states. "Revenue on 15 March 2017"
  against a window for 1 June 2018 is `low_confidence` with a `time_window_unrealized` gap, where
  it was `ok`; hours within the stated day still agree. A year is never consumed because a
  window's bounds hold it, its exclusive end year included: "revenue 2018" against a 2017 window
  and "at 2000" are left over in `why.details.terms`. A year counts as part of a phrase plan could
  not resolve only after a bound or qualifier word ("before 2017", "the end of 2017"). In a
  question over 2,000 characters, only a single 20xx year after "in", "for", "during" or "year"
  is checked, as a calendar year; two different years, or a count such as "in 2000 or more",
  state no window and are left over. A window you pass is never held to a lone "previous month"
  when the draft carries a `prior_period` expression.
