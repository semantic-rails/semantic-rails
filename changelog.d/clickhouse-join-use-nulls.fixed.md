- ClickHouse statements now end with `SETTINGS join_use_nulls = 1`. Without it an unmatched
  outer-join field read its type's default (`0` or an empty string) instead of `NULL`, so a
  group one measure lacked could read as data of nothing, and the combined time key of a
  multi-measure query could read `0`.
