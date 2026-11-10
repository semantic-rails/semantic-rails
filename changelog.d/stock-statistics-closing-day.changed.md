- Every statistic of a balance asked with no time window reads the last closing day, as the
  balance itself (`last_value`, `sum`) and a `distribution` over its series already did, and
  the answer names that day with `BALANCE_CLOSING_DAY`. `avg`, `median`, `percentile`, `min`
  and `max` of a stock on an `as_of_time` clock used to read each series' own last snapshot,
  however old, so one stock gave two numbers: the average inventory per store read `967.2`,
  counting a store whose last snapshot is from 2016, beside a closing-day inventory of `925`;
  it now reads `925`. A series with no snapshot on that day is left out of every statistic
  (`NULL` when it is the only one filtered to), and a balance keyed by its clock alone reads
  that day's snapshot instead of every snapshot it has; give a window for a statistic over
  time. Answers with a window, a grain or a grouping by the clock are unchanged. See
  [Balances](docs/QUERY_CALL.md#balances).
