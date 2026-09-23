- `semantic-rails ask` shows the engine's warnings and assumptions, and says when the row
  limit cut the result short (`result.truncated` in `--json`; `--limit 0` fetches every row).
  Its tables use column labels, thousands separators and consistent decimals, right-align
  numbers and never cut them off.
