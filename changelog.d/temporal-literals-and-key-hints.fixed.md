- Preserve warehouse-supported date and timestamp filter literals, and locate unambiguous
  DuckDB temporal cast failures at the caller's filter value without exposing driver text.
- Keep visible nonadditive measure key hints when grouping dimensions are written in `select`.
