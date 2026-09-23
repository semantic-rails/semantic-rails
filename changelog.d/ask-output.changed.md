- `semantic-rails ask` shows the engine's warnings and assumptions, and says when the row
  cap cut the result short (`result.truncated` in `--json`; `--limit 0` removes the cap) or
  the planned query carries its own limit (`result.planned_limit`). Its tables use column
  labels, thousands separators and consistent decimals for measures, print IDs and years as
  stored, right-align numbers, and never cut a number off or show a nonzero value as zero;
  a column of very small values prints about three significant digits.
