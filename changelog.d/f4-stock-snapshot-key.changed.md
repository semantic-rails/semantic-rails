- A `stock` measure whose entity key doesn't contain its clock's column now gets a
  `STOCK_SNAPSHOT_KEY_MISSING_CLOCK` parse warning. Keyed by a surrogate that is unique per
  snapshot row, every snapshot counted as its own series, so a week holding two daily snapshots
  of one series returned their sum. **Upgrade note:** on an `as_of_time` clock such queries are
  now refused with `INVALID_CONFIG` instead of returning that sum. Key the entity by the series
  columns plus the clock's column (for example `key: [store_id, date_day]`).
