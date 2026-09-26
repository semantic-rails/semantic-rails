- A `stock` measure whose row key (its declared grain, else its entity key) doesn't contain its
  clock's column now gets a `STOCK_SNAPSHOT_KEY_MISSING_CLOCK` parse warning. Keyed by a
  surrogate that is unique per snapshot row, every snapshot counted as its own series, so a week
  holding two daily snapshots of one series returned their sum. **Upgrade note:** on an
  `as_of_time` clock such queries are now refused with `INVALID_CONFIG` instead of returning that
  sum, so `project validate` and `semantic-rails check` report a failed probe for the measure until
  the entity is keyed by the series columns plus the clock's column (for example
  `key: [store_id, date_day]`).
