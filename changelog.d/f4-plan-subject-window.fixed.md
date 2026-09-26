- `plan` no longer reports `ok` when it answers a question about a period with a stock or rolling
  metric that carries its own trailing window. "Unique visitors this week" drafted "Unique visitors
  (14 days)" filtered to this week and returned the 14-day count as this week's number. Such drafts
  are now `low_confidence` with a `subject_window_mismatch` gap, unless the question names the same
  window. The window is read from the subject's label, name or id.
