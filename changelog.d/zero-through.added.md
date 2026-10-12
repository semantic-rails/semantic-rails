- A package can say how far each table is loaded with `zero_through`, on a model or once in
  `defaults.yml`: `data` (the default, the last row), `now`, or
  `{loaded: <model>.<field>}`, the latest date or timestamp a load log records. A quiet
  week that the load covers then reads 0 instead of NULL, and after a nightly that ran, so
  does a quiet "this month so far" (only today is unloaded); a period after the edge reads NULL
  with `NO_DATA_YET` ("its data is loaded through <date>"), and
  an empty load log reads NULL ("loaded-through date unavailable"). `window.data_through` is
  the declared date, and `window.data_through_source` names the load log (`loaded` to a caller
  who can't see it) or `now`. `now` adds the note `ZERO_AFTER_LAST_ROW`. A `row_filter` on the
  load log refuses the query rather than read it unfiltered; ClickHouse packages may only
  declare `data`. See "Empty periods" in `docs/PACKAGE_AUTHORING.md`.
- `NO_DATA_YET` under the last-row edge now says how to declare a load log ("If the load ran
  and nothing happened, declare zero_through").
- An entity filter whose threshold 0 passes, taken per period or over the window, on a
  measure whose table declares `zero_through` is refused (`INVALID_METRIC_PREDICATE`) rather
  than counting a period after the edge as 0. With `sql_profile: "off"`, no response names a
  load log the caller can't see, and the full `explain` leaves out the SQL too.
- A declared edge caps what a model's measures read: no row after the load log's mark (or
  after `now`) is read by any series, total, running total, share, prior period or entity
  filter, so a stray row past the edge reads NULL like the rest of its period, and an empty
  load log reads nothing. Such measures read their base table, never a rollup.
