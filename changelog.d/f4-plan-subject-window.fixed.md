- `plan` no longer reports `ok` when it answers a question about a period with a stock that
  carries its own trailing window. "Unique visitors this week" drafted "Unique visitors (14 days)"
  filtered to this week and returned the 14-day count as this week's number. When a stock's
  label, name or id states a span and each row the draft reports covers a period of another
  length, the plan is now `low_confidence` with a `subject_window_mismatch` gap, whatever the
  question says, so a daily read of a rolling-window stock is flagged too.
