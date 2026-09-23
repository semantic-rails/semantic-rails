- `semantic-rails ask` shows the engine's warnings and assumptions, and says when the row
  limit cut the result short (`result.truncated` in `--json`; `--limit 0` fetches every row).
  Its tables use column labels, thousands separators and consistent decimals for measures,
  print IDs and years as stored, right-align numbers, and never cut a number off or show a
  nonzero value as zero.
