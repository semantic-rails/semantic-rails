- The Architect's `suggest_model`, dbt import suggestions and their `upsert_model` drafts give a
  time column named like a snapshot's as-of time (`snapshot_date`, `as_of_date`) `class:
  as_of_time` instead of `event_time`. A `stock` measure on such a clock whose key doesn't contain
  it is refused instead of summing every snapshot.
