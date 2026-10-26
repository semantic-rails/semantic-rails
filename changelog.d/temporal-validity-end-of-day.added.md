- A relationship's `temporal_validity` takes `as_of: end_of_day`: every hop into the history
  through that relationship reads the version in force at the end of the row's day on the query's
  clock, instead of at the row's own time. Use it for a history written a moment after the row it
  describes: "new workspaces by the plan they signed up on" then reads the plan in force at the
  end of the signup day, instead of an empty Plan for every workspace whose plan was written
  seconds later. A plan changed on the signup day reads the day's last plan. `as_of: time` (the
  default) keeps today's answers. Route readings and `NULL_PRESERVING_HISTORY` say "valid at the
  end of the day", and `temporal_validity` refuses unknown keys and an `as_of` with neither
  `valid_from` nor `valid_to`. Checked on DuckDB and Postgres; see the zone note in
  `docs/PACKAGE_AUTHORING.md` for the other warehouses.
